import os
import hmac
import time
import base64
import hashlib
from typing import Optional
from fastapi import FastAPI, HTTPException, Query, Request, Depends, BackgroundTasks
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.worker import (
    run_sync,
    regenerate_lead_reply,
    get_db,
    get_setting,
    test_single_gemini_key,
)

app = FastAPI(title="CodeXplus Lead Engine")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

COOKIE_NAME = "cx_lead_session"
SESSION_TTL = 60 * 60 * 24 * 7  # 7 days


def get_secret() -> bytes:
    secret = os.environ.get("APP_SECRET", "codexplus-default-secret-change-me")
    return secret.encode("utf-8")


def get_admin_credentials() -> tuple[str, str]:
    user = get_setting("admin_username", os.environ.get("ADMIN_USERNAME", "admin"))
    pw = get_setting("admin_password", os.environ.get("ADMIN_PASSWORD", "CodeXplus@2026"))
    return user, pw


def create_session_token(username: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    payload = f"{username}:{exp}".encode("utf-8")
    sig = hmac.new(get_secret(), payload, hashlib.sha256).hexdigest()
    raw = f"{username}:{exp}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def verify_session_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        username, exp_str, sig = decoded.split(":", 2)
        if int(exp_str) < int(time.time()):
            return None
        expected_sig = hmac.new(
            get_secret(), f"{username}:{exp_str}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        return username
    except Exception:
        return None


def require_auth(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    user = verify_session_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


class LoginPayload(BaseModel):
    username: str
    password: str


class LeadUpdate(BaseModel):
    status: Optional[str] = None
    suggested_reply: Optional[str] = None
    notes: Optional[str] = None


class SettingsUpdate(BaseModel):
    serper_api_key: Optional[str] = None
    gemini_api_key_1: Optional[str] = None
    gemini_api_key_2: Optional[str] = None
    gemini_api_key_3: Optional[str] = None
    gemini_api_key_4: Optional[str] = None
    reddit_query: Optional[str] = None
    x_query: Optional[str] = None
    sync_interval_minutes: Optional[int] = None
    admin_username: Optional[str] = None
    admin_password: Optional[str] = None


class KeyTestPayload(BaseModel):
    api_key: str


def serialize_lead(row) -> dict:
    item = dict(row)
    post_link = item.get("url") or "#"
    item["url"] = post_link
    item["post_url"] = post_link
    item["link"] = post_link
    return item


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = verify_session_token(request.cookies.get(COOKIE_NAME))
    return {"authenticated": bool(user), "username": user}


@app.post("/api/auth/login")
def auth_login(payload: LoginPayload):
    expected_user, expected_pw = get_admin_credentials()
    if not (
        hmac.compare_digest(payload.username.strip(), expected_user)
        and hmac.compare_digest(payload.password, expected_pw)
    ):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = create_session_token(expected_user)
    resp = JSONResponse({"authenticated": True, "username": expected_user})
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL,
    )
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    resp = JSONResponse({"authenticated": False})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/api/leads")
def list_leads(
    status: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    min_score: int = Query(0),
    region: Optional[str] = Query(None),
    has_budget: bool = Query(False),
    _user: str = Depends(require_auth),
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

    query += " ORDER BY created_at DESC LIMIT 250"
    cur.execute(query, params)
    rows = [serialize_lead(row) for row in cur.fetchall()]

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
def update_lead(lead_id: int, payload: LeadUpdate, _user: str = Depends(require_auth)):
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
    updated = serialize_lead(cur.fetchone())
    conn.close()
    return {"lead": updated}


@app.delete("/api/leads/{lead_id}")
def delete_lead(lead_id: int, _user: str = Depends(require_auth)):
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
def bulk_delete_leads(status: Optional[str] = Query(None), _user: str = Depends(require_auth)):
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
def regenerate_reply(lead_id: int, _user: str = Depends(require_auth)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Lead not found")

    lead = serialize_lead(row)
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
def trigger_sync(background_tasks: BackgroundTasks, _user: str = Depends(require_auth)):
    background_tasks.add_task(run_sync)
    return {
        "fetched": 0,
        "saved": 0,
        "qualified": 0,
        "notes": "Background sync started! Wait 20-30 seconds and refresh to see new leads.",
        "errors": [],
    }


@app.get("/api/admin/settings")
def get_admin_settings(_user: str = Depends(require_auth)):
    keys = [
        "serper_api_key",
        "gemini_api_key_1",
        "gemini_api_key_2",
        "gemini_api_key_3",
        "gemini_api_key_4",
        "reddit_query",
        "x_query",
        "sync_interval_minutes",
        "admin_username",
    ]
    data = {}
    for k in keys:
        if k == "admin_username":
            data[k] = get_setting(k, os.environ.get("ADMIN_USERNAME", "admin"))
        else:
            data[k] = get_setting(k, "")
    return {"settings": data}


@app.post("/api/admin/settings")
def save_admin_settings(payload: SettingsUpdate, _user: str = Depends(require_auth)):
    conn = get_db()
    cur = conn.cursor()
    updates = {
        "serper_api_key": payload.serper_api_key,
        "gemini_api_key_1": payload.gemini_api_key_1,
        "gemini_api_key_2": payload.gemini_api_key_2,
        "gemini_api_key_3": payload.gemini_api_key_3,
        "gemini_api_key_4": payload.gemini_api_key_4,
        "reddit_query": payload.reddit_query,
        "x_query": payload.x_query,
        "sync_interval_minutes": str(payload.sync_interval_minutes) if payload.sync_interval_minutes is not None else None,
        "admin_username": payload.admin_username.strip() if payload.admin_username else None,
        "admin_password": payload.admin_password if payload.admin_password else None,
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


@app.post("/api/admin/test-gemini-key")
def test_gemini_key_endpoint(payload: KeyTestPayload, _user: str = Depends(require_auth)):
    return test_single_gemini_key(payload.api_key)