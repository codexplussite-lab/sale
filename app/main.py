import os
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.worker import run_sync, regenerate_lead_reply, get_db

app = FastAPI(title="CodeXplus Lead Engine")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LeadUpdate(BaseModel):
    status: Optional[str] = None
    suggested_reply: Optional[str] = None
    notes: Optional[str] = None


class SettingsUpdate(BaseModel):
    serper_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    reddit_query: Optional[str] = None
    x_query: Optional[str] = None
    sync_interval_minutes: Optional[int] = None


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/api/leads")
def list_leads(
    status: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    min_score: int = Query(0),
    region: Optional[str] = Query(None),
    has_budget: bool = Query(False),
):
    conn = get_db()
    cur = conn.cursor()

    query = "SELECT * FROM leads WHERE score >= ?"
    params = [min_score]

    if status and status != "all":
        query += " AND status = ?"
        params.append(status)

    if platform and platform != "all":
        query += " AND LOWER(platform) = LOWER(?)"
        params.append(platform)

    if has_budget:
        query += " AND (raw_text LIKE '%$%' OR summary LIKE '%$%' OR LOWER(raw_text) LIKE '%budget%' OR LOWER(summary) LIKE '%budget%')"

    if region == "usa":
        query += " AND (LOWER(raw_text) LIKE '%usa%' OR LOWER(raw_text) LIKE '%u.s.%' OR LOWER(raw_text) LIKE '%united states%' OR LOWER(raw_text) LIKE '%est%' OR LOWER(raw_text) LIKE '%pst%' OR LOWER(raw_text) LIKE '%cst%' OR LOWER(raw_text) LIKE '%usd%' OR raw_text LIKE '%$%')"
    elif region == "europe":
        query += " AND (LOWER(raw_text) LIKE '%uk%' OR LOWER(raw_text) LIKE '%united kingdom%' OR LOWER(raw_text) LIKE '%europe%' OR LOWER(raw_text) LIKE '%eu%' OR LOWER(raw_text) LIKE '%gbp%' OR LOWER(raw_text) LIKE '%eur%' OR raw_text LIKE '%£%' OR raw_text LIKE '%€%')"

    query += " ORDER BY created_at DESC LIMIT 200"
    cur.execute(query, params)
    rows = [dict(row) for row in cur.fetchall()]

    # Calculate stats expected by the frontend dashboard
    cur.execute("SELECT COUNT(*) FROM leads")
    total_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM leads WHERE status = 'new'")
    new_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM leads WHERE score >= 7")
    high_score_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM leads WHERE status = 'replied'")
    replied_count = cur.fetchone()[0]

    conn.close()
    return {
        "leads": rows,
        "stats": {
            "total": total_count,
            "new": new_count,
            "high_score": high_score_count,
            "qualified": high_score_count,
            "replied": replied_count,
        },
    }


@app.patch("/api/leads/{lead_id}")
def update_lead(lead_id: int, payload: LeadUpdate):
    conn = get_db()
    cur = conn.cursor()

    updates = []
    params = []

    if payload.status is not None:
        updates.append("status = ?")
        params.append(payload.status)
    if payload.suggested_reply is not None:
        updates.append("suggested_reply = ?")
        params.append(payload.suggested_reply)
    if payload.notes is not None:
        updates.append("notes = ?")
        params.append(payload.notes)

    if not updates:
        conn.close()
        raise HTTPException(status_code=400, detail="No fields to update")

    params.append(lead_id)
    cur.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", params)
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Lead not found")

    conn.commit()
    cur.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
    updated = dict(cur.fetchone())
    conn.close()
    return {"lead": updated}


@app.delete("/api/leads/{lead_id}")
def delete_lead(lead_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Lead not found")
    conn.commit()
    conn.close()
    return {"deleted": True, "id": lead_id}


@app.delete("/api/leads")
def bulk_delete_leads(status: Optional[str] = Query(None)):
    conn = get_db()
    cur = conn.cursor()
    if status and status != "all":
        cur.execute("DELETE FROM leads WHERE status = ?", (status,))
    else:
        cur.execute("DELETE FROM leads")
    deleted_count = cur.rowcount
    conn.commit()
    conn.close()
    return {"deleted_count": deleted_count}


@app.post("/api/leads/{lead_id}/regenerate")
def regenerate_reply(lead_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Lead not found")

    lead = dict(row)
    try:
        new_reply = regenerate_lead_reply(lead)
    except Exception as exc:
        conn.close()
        raise HTTPException(status_code=400, detail=str(exc))

    cur.execute("UPDATE leads SET suggested_reply = ? WHERE id = ?", (new_reply, lead_id))
    conn.commit()
    conn.close()
    return {"id": lead_id, "suggested_reply": new_reply}


@app.post("/api/sync")
def trigger_sync():
    result = run_sync()
    notes_str = result.get("notes", "")
    errors_list = [n.strip() for n in notes_str.split(";") if n.strip()] if notes_str else []
    result["errors"] = errors_list
    return result


@app.get("/api/settings")
def get_settings():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings")
    data = {row["key"]: row["value"] for row in cur.fetchall()}
    conn.close()
    return {"settings": data}


@app.post("/api/settings")
def save_settings(payload: SettingsUpdate):
    conn = get_db()
    cur = conn.cursor()
    updates = {
        "serper_api_key": payload.serper_api_key,
        "gemini_api_key": payload.gemini_api_key,
        "reddit_query": payload.reddit_query,
        "x_query": payload.x_query,
        "sync_interval_minutes": str(payload.sync_interval_minutes) if payload.sync_interval_minutes is not None else None,
    }
    for k, v in updates.items():
        if v is not None:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, v),
            )
    conn.commit()
    conn.close()
    return {"saved": True}