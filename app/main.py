import os
import threading
from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.worker import (
    AGENTROUTER_DEFAULT_BASE,
    DEFAULT_AGENT_PROMPT,
    BROAD_BUYER_DORK,
    get_db,
    get_setting,
    set_setting,
    get_scan_status,
    clear_seen_cache,
    run_sync,
    regenerate_lead_reply,
    generate_queries_from_prompt,
    test_single_gemini_key,
    test_agentrouter_key,
)

app = FastAPI(title="CodeXplus Lead Engine")
templates = Jinja2Templates(directory="app/templates")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "codexplus2026")


class SyncRequest(BaseModel):
    prompt: str | None = None
    freshness: str | None = None
    platform: str = "all"


class LeadUpdate(BaseModel):
    status: str | None = None
    notes: str | None = None
    suggested_reply: str | None = None


class SettingsUpdate(BaseModel):
    agent_prompt: str | None = None
    agent_subreddits: str | None = None
    agent_reddit_keywords: str | None = None
    x_query: str | None = None
    search_freshness: str | None = None
    agent_min_score: str | None = None
    serper_api_key: str | None = None
    gemini_api_key_1: str | None = None
    gemini_api_key_2: str | None = None
    gemini_api_key_3: str | None = None
    gemini_api_key_4: str | None = None
    agentrouter_api_key: str | None = None
    agentrouter_base_url: str | None = None


def _is_authed(request: Request) -> bool:
    return request.cookies.get("cx_auth") == ADMIN_PASSWORD


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _is_authed(request):
        return templates.TemplateResponse("login.html", {"request": request, "error": ""})

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM leads ORDER BY score DESC, id DESC LIMIT 200")
    leads = [dict(r) for r in cur.fetchall()]
    conn.close()

    ctx = {
        "request": request,
        "leads": leads,
        "agent_prompt": get_setting("agent_prompt", DEFAULT_AGENT_PROMPT),
        "agent_subreddits": get_setting(
            "agent_subreddits",
            "forhire,freelance_forhire,DesignJobs,slavelabour,startups,SaaS,shopify,smallbusiness,Entrepreneur",
        ),
        "agent_reddit_keywords": get_setting(
            "agent_reddit_keywords",
            "[hiring],looking to hire,looking for a,need a,want to build,create my,build my,i will pay,budget",
        ),
        "x_query": get_setting("x_query", BROAD_BUYER_DORK),
        "search_freshness": get_setting("search_freshness", "qdr:h"),
        "agent_min_score": get_setting("agent_min_score", "7"),
        "serper_api_key": get_setting("serper_api_key", ""),
        "gemini_api_key_1": get_setting("gemini_api_key_1", ""),
        "gemini_api_key_2": get_setting("gemini_api_key_2", ""),
        "gemini_api_key_3": get_setting("gemini_api_key_3", ""),
        "gemini_api_key_4": get_setting("gemini_api_key_4", ""),
        "agentrouter_api_key": get_setting("agentrouter_api_key", ""),
        "agentrouter_base_url": get_setting("agentrouter_base_url", AGENTROUTER_DEFAULT_BASE),
        "scan_status": get_scan_status(),
    }
    return templates.TemplateResponse("dashboard.html", ctx)


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("cx_auth", ADMIN_PASSWORD, httponly=True, max_age=86400 * 30)
        return resp
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid password"})


@app.post("/logout")
def logout():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("cx_auth")
    return resp


@app.get("/api/leads")
def api_leads(request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM leads ORDER BY score DESC, id DESC LIMIT 200")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"leads": rows}


@app.patch("/api/leads/{lead_id}")
def update_lead(lead_id: int, payload: LeadUpdate, request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db()
    cur = conn.cursor()
    if payload.status is not None:
        cur.execute("UPDATE leads SET status = ? WHERE id = ?", (payload.status, lead_id))
    if payload.notes is not None:
        cur.execute("UPDATE leads SET notes = ? WHERE id = ?", (payload.notes, lead_id))
    if payload.suggested_reply is not None:
        cur.execute("UPDATE leads SET suggested_reply = ? WHERE id = ?", (payload.suggested_reply, lead_id))
    conn.close()
    return {"ok": True}


@app.post("/api/leads/{lead_id}/regenerate")
def api_regenerate_reply(lead_id: int, request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Lead not found")
    new_reply = regenerate_lead_reply(dict(row))
    cur.execute("UPDATE leads SET suggested_reply = ? WHERE id = ?", (new_reply, lead_id))
    conn.close()
    return {"ok": True, "reply": new_reply}


@app.get("/api/scan/status")
def api_scan_status(request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return get_scan_status()


@app.post("/api/sync")
def api_sync(payload: SyncRequest, request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    status = get_scan_status()
    if status.get("running"):
        return JSONResponse({"ok": True, "started": False, "status": status})

    t = threading.Thread(
        target=run_sync,
        kwargs={
            "custom_prompt": payload.prompt,
            "freshness": payload.freshness,
            "platform": payload.platform,
        },
        daemon=True,
    )
    t.start()
    return JSONResponse({"ok": True, "started": True, "status": get_scan_status()})


@app.post("/api/settings")
def api_save_settings(payload: SettingsUpdate, request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        set_setting(k, str(v).strip())
    return {"ok": True}


@app.post("/api/generate-dorks")
def api_generate_dorks(payload: dict, request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    prompt_txt = str(payload.get("prompt", "")).strip() or get_setting("agent_prompt", DEFAULT_AGENT_PROMPT)
    generated = generate_queries_from_prompt(prompt_txt)
    set_setting("agent_subreddits", generated["subreddits"])
    set_setting("agent_reddit_keywords", generated["reddit_keywords"])
    set_setting("x_query", generated["serper_dork"])
    return {"ok": True, **generated}


@app.post("/api/reset-seen")
def api_reset_seen(request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    cleared = clear_seen_cache()
    return {"ok": True, "cleared": cleared}


@app.post("/api/test-gemini")
def api_test_gemini(payload: dict, request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    key_val = str(payload.get("api_key", "")).strip()
    return test_single_gemini_key(key_val)


@app.post("/api/test-agentrouter")
def api_test_agentrouter(payload: dict, request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    key_val = str(payload.get("api_key", "")).strip()
    base_val = str(payload.get("base_url", AGENTROUTER_DEFAULT_BASE)).strip()
    return test_agentrouter_key(key_val, base_val)