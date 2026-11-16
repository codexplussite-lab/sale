import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.db import get_settings_dict, init_db, pool
from app.worker import run_lead_sync, score_with_gemini

logging.basicConfig(level=logging.INFO)
scheduler = BackgroundScheduler()
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    settings = get_settings_dict()
    interval = max(5, int(settings.get("sync_interval_minutes", "20")))
    scheduler.add_job(run_lead_sync, "interval", minutes=interval, id="lead_sync_job", replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
    pool.close()


app = FastAPI(title="Software House Lead Engine", lifespan=lifespan)


class LeadUpdate(BaseModel):
    status: Optional[str] = None
    drafted_reply: Optional[str] = None


class SettingsUpdate(BaseModel):
    reddit_query: str
    x_query: str
    min_intent_score: int
    sync_interval_minutes: int


@app.get("/")
def serve_dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/leads")
def list_leads(
    status: Optional[str] = Query(default=None),
    platform: Optional[str] = Query(default=None),
    min_score: int = Query(default=0),
    region: Optional[str] = Query(default=None),
    has_budget: bool = Query(default=False),
    q: Optional[str] = Query(default=None),
):
    clauses = ["intent_score >= %s"]
    params: list[object] = [min_score]

    if status and status != "all":
        clauses.append("status = %s")
        params.append(status)
    if platform and platform != "all":
        clauses.append("platform = %s")
        params.append(platform)
    if region == "usa":
        clauses.append(
            "(raw_text ~* '\\\\m(USA|US|United States|USD|EST|PST|CST|MST|New York|California|Texas|Florida)\\\\M' OR raw_text ILIKE '%%$%%')"
        )
    elif region == "uk_eu":
        clauses.append(
            "(raw_text ~* '\\\\m(UK|United Kingdom|London|Europe|EU|Germany|France|GBP|EUR)\\\\M' OR raw_text ILIKE '%%£%%' OR raw_text ILIKE '%%€%%')"
        )
    if has_budget:
        clauses.append(
            "(raw_text ~* '(\\$|USD|EUR|GBP|£|€|budget|paying|rate|/hr|fixed price)' OR pain_point_summary ~* '(\\$|USD|budget)')"
        )
    if q:
        clauses.append("(raw_text ILIKE %s OR pain_point_summary ILIKE %s OR author_handle ILIKE %s)")
        like = f"%{q}%"
        params.extend([like, like, like])

    where_sql = " AND ".join(clauses)

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, platform, platform_post_id, author_handle, post_url,
                       raw_text, intent_score, project_category, pain_point_summary,
                       drafted_reply, status, created_at
                FROM social_leads
                WHERE {where_sql}
                ORDER BY created_at DESC, intent_score DESC
                LIMIT 200;
                """,
                params,
            )
            rows = cur.fetchall()

            cur.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE intent_score >= 7) AS qualified,
                  COUNT(*) FILTER (WHERE status = 'pending_review') AS pending,
                  COUNT(*) FILTER (WHERE status = 'replied') AS replied
                FROM social_leads;
                """
            )
            stats_row = cur.fetchone()

    leads = [
        {
            "id": r[0],
            "platform": r[1],
            "platform_post_id": r[2],
            "author_handle": r[3],
            "post_url": r[4],
            "raw_text": r[5],
            "intent_score": r[6],
            "project_category": r[7],
            "pain_point_summary": r[8],
            "drafted_reply": r[9],
            "status": r[10],
            "created_at": r[11].isoformat() if r[11] else None,
        }
        for r in rows
    ]

    return {
        "leads": leads,
        "stats": {
            "total": stats_row[0],
            "qualified": stats_row[1],
            "pending": stats_row[2],
            "replied": stats_row[3],
        },
    }


@app.post("/api/leads/{lead_id}/regenerate")
def regenerate_lead_reply(lead_id: int):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT platform, author_handle, raw_text FROM social_leads WHERE id = %s;",
                (lead_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Lead not found")

        lead = {"platform": row[0], "author_handle": row[1] or "", "raw_text": row[2] or ""}
        try:
            scored = score_with_gemini(lead)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Gemini error: {exc}")

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE social_leads
                SET drafted_reply = %s, pain_point_summary = %s, updated_at = NOW()
                WHERE id = %s;
                """,
                (scored["drafted_reply"], scored["pain_point_summary"], lead_id),
            )
        conn.commit()

    return {
        "id": lead_id,
        "drafted_reply": scored["drafted_reply"],
        "pain_point_summary": scored["pain_point_summary"],
    }


@app.patch("/api/leads/{lead_id}")
def update_lead(lead_id: int, payload: LeadUpdate):
    updates = []
    params: list[object] = []

    if payload.status is not None:
        if payload.status not in {"pending_review", "replied", "archived"}:
            raise HTTPException(status_code=400, detail="Invalid status")
        updates.append("status = %s")
        params.append(payload.status)

    if payload.drafted_reply is not None:
        updates.append("drafted_reply = %s")
        params.append(payload.drafted_reply)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updates.append("updated_at = NOW()")
    params.append(lead_id)

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE social_leads SET {', '.join(updates)} WHERE id = %s RETURNING id;",
                params,
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Lead not found")
        conn.commit()
    return {"updated": True, "id": lead_id}


@app.delete("/api/leads/{lead_id}")
def delete_lead(lead_id: int):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM social_leads WHERE id = %s RETURNING id;", (lead_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Lead not found")
        conn.commit()
    return {"deleted": True, "id": lead_id}


@app.delete("/api/leads")
def bulk_delete_leads(status: Optional[str] = Query(default=None)):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if status and status != "all":
                cur.execute("DELETE FROM social_leads WHERE status = %s;", (status,))
            else:
                cur.execute("DELETE FROM social_leads;")
            deleted_count = cur.rowcount
        conn.commit()
    return {"deleted": deleted_count}


@app.post("/api/sync")
def trigger_sync():
    return run_lead_sync()


@app.get("/api/settings")
def read_settings():
    settings = get_settings_dict()
    return {
        **settings,
        "configured": {
            "gemini": bool(os.getenv("GEMINI_API_KEY", "").strip()),
            "serper": bool(os.getenv("SERPER_API_KEY", "").strip()),
            "x": bool(os.getenv("X_BEARER_TOKEN", "").strip()),
            "discord": bool(os.getenv("DISCORD_WEBHOOK_URL", "").strip()),
        },
    }


@app.put("/api/settings")
def update_settings(payload: SettingsUpdate):
    interval = max(5, payload.sync_interval_minutes)
    pairs = {
        "reddit_query": payload.reddit_query,
        "x_query": payload.x_query,
        "min_intent_score": str(payload.min_intent_score),
        "sync_interval_minutes": str(interval),
    }
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for k, v in pairs.items():
                cur.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
                    """,
                    (k, v),
                )
        conn.commit()

    scheduler.reschedule_job("lead_sync_job", trigger="interval", minutes=interval)
    return {"saved": True}