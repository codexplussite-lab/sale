import os
import html
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from app.worker import (
    get_db,
    get_setting,
    set_setting,
    get_active_provider,
    get_agentrouter_config,
    test_agentrouter_key,
    test_single_gemini_key,
    clear_seen_cache,
    run_sync,
    get_scan_status,
    regenerate_lead_reply,
    hash_password,
    verify_password,
    DEFAULT_AGENT_PROMPT,
    BROAD_BUYER_DORK,
)

app = FastAPI(title="CodeXplus Lead Engine")
SECRET_KEY = os.environ.get("SECRET_KEY", "codexplus-lead-engine-secret-key-2026")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="cxp_session", max_age=86400 * 14)

SVG_FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#6366f1"/><stop offset="100%" stop-color="#06b6d4"/></linearGradient></defs>
  <rect width="64" height="64" rx="16" fill="url(#g)"/>
  <path d="M24 20L12 32l12 12M40 20l12 12-12 12M35 16l-6 32" stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
</svg>"""


def get_current_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    conn = get_db()
    row = conn.execute("SELECT id, name, email, role, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not row or not row["is_active"]:
        request.session.clear()
        return None
    return dict(row)


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=SVG_FAVICON, media_type="image/svg+xml")


@app.get("/health")
def health():
    return {"status": "ok", "provider": get_active_provider()}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=302)
    err_html = f'<div class="alert-err">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Sign In · CodeXplus Lead Engine</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml"/>
<style>
  :root {{ --bg:#070b14; --card:#0f172a; --border:#1e293b; --text:#f8fafc; --muted:#94a3b8; --accent:#6366f1; --cyan:#06b6d4; }}
  * {{ box-sizing:border-box; font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; }}
  body {{ margin:0; min-height:100vh; background:radial-gradient(circle at top right, #1e1b4b 0%, var(--bg) 55%); color:var(--text); display:flex; align-items:center; justify-content:center; padding:24px; }}
  .login-card {{ width:100%; max-width:420px; background:rgba(15,23,42,0.92); backdrop-filter:blur(16px); border:1px solid var(--border); border-radius:20px; padding:36px; box-shadow:0 25px 50px -12px rgba(0,0,0,0.65); }}
  .brand {{ display:flex; align-items:center; gap:12px; margin-bottom:24px; }}
  .logo {{ width:42px; height:42px; border-radius:12px; background:linear-gradient(135deg, var(--accent), var(--cyan)); display:flex; align-items:center; justify-content:center; font-weight:800; font-size:18px; }}
  h1 {{ margin:0; font-size:20px; font-weight:700; }}
  p.sub {{ margin:4px 0 0; color:var(--muted); font-size:13px; }}
  label {{ display:block; font-size:12px; font-weight:600; color:#cbd5e1; margin:16px 0 6px; text-transform:uppercase; letter-spacing:0.04em; }}
  input {{ width:100%; padding:12px 14px; border-radius:10px; border:1px solid #334155; background:#020617; color:var(--text); font-size:14px; outline:none; transition:border 0.2s; }}
  input:focus {{ border-color:var(--accent); }}
  button {{ width:100%; margin-top:24px; padding:12px; border:none; border-radius:10px; background:linear-gradient(135deg, var(--accent), #4f46e5); color:#fff; font-weight:600; font-size:14px; cursor:pointer; transition:transform 0.1s, opacity 0.2s; }}
  button:hover {{ opacity:0.95; }}
  .alert-err {{ background:rgba(239,68,68,0.14); border:1px solid rgba(239,68,68,0.4); color:#fca5a5; padding:10px 12px; border-radius:10px; font-size:13px; margin-bottom:14px; }}
  .hint {{ margin-top:18px; text-align:center; font-size:12px; color:var(--muted); }}
</style>
</head>
<body>
  <div class="login-card">
    <div class="brand">
      <div class="logo">&lt;/&gt;</div>
      <div>
        <h1>CodeXplus Lead Engine</h1>
        <p class="sub">Sign in to access live buyer intelligence</p>
      </div>
    </div>
    {err_html}
    <form method="post" action="/login">
      <label>Email Address</label>
      <input type="email" name="email" placeholder="admin@codexplus.site" required autofocus/>
      <label>Password</label>
      <input type="password" name="password" placeholder="••••••••••••" required/>
      <button type="submit">Sign In to Workspace</button>
    </form>
    <div class="hint">CodeXplus Agency Suite · Protected Workspace</div>
  </div>
</body>
</html>"""


@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    clean_email = email.strip().lower()
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (clean_email,)).fetchone()
    conn.close()
    if not row or not row["is_active"] or not verify_password(password, row["password_hash"]):
        return RedirectResponse(url="/login?error=Invalid+email+or+password", status_code=302)
    request.session["user_id"] = row["id"]
    return RedirectResponse(url="/", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, msg: str = "", err: str = ""):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    conn = get_db()
    all_users = conn.execute("SELECT id, name, email, role, is_active, created_at FROM users ORDER BY id ASC").fetchall()
    conn.close()

    rows_html = ""
    for u in all_users:
        badge_role = '<span class="pill pill-admin">Admin</span>' if u["role"] == "admin" else '<span class="pill pill-member">Member</span>'
        badge_status = '<span class="pill pill-ok">Active</span>' if u["is_active"] else '<span class="pill pill-off">Disabled</span>'
        actions = ""
        if user["role"] == "admin" and u["id"] != user["id"]:
            toggle_label = "Disable" if u["is_active"] else "Enable"
            actions = f"""
            <form method="post" action="/users/{u['id']}/toggle" style="display:inline;">
              <button class="btn-sm" type="submit">{toggle_label}</button>
            </form>
            <form method="post" action="/users/{u['id']}/delete" style="display:inline;" onsubmit="return confirm('Delete this user?');">
              <button class="btn-sm btn-danger" type="submit">Delete</button>
            </form>
            """
        elif u["id"] == user["id"]:
            actions = '<span style="color:#64748b;font-size:12px;">Current Session</span>'

        rows_html += f"""
        <tr>
          <td><strong>{html.escape(u['name'])}</strong></td>
          <td>{html.escape(u['email'])}</td>
          <td>{badge_role}</td>
          <td>{badge_status}</td>
          <td style="color:#94a3b8;font-size:12px;">{(u['created_at'] or '')[:10]}</td>
          <td style="text-align:right;">{actions}</td>
        </tr>
        """

    banner = ""
    if msg:
        banner = f'<div class="banner-ok">{html.escape(msg)}</div>'
    elif err:
        banner = f'<div class="banner-err">{html.escape(err)}</div>'

    create_panel = ""
    if user["role"] == "admin":
        create_panel = """
        <div class="card">
          <h3>Add Team Member</h3>
          <form method="post" action="/users/create" class="grid-form">
            <div>
              <label>Full Name</label>
              <input type="text" name="name" placeholder="Syed Jawwad" required/>
            </div>
            <div>
              <label>Email Address</label>
              <input type="email" name="email" placeholder="member@codexplus.site" required/>
            </div>
            <div>
              <label>Password</label>
              <input type="password" name="password" placeholder="Min 6 characters" minlength="6" required/>
            </div>
            <div>
              <label>Role</label>
              <select name="role">
                <option value="member">Member (View & Outreach)</option>
                <option value="admin">Admin (Full Access)</option>
              </select>
            </div>
            <div style="display:flex;align-items:flex-end;">
              <button type="submit" class="btn-primary" style="width:100%;">Create User</button>
            </div>
          </form>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Team & Users · CodeXplus Lead Engine</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml"/>
<style>
  :root {{ --bg:#070b14; --card:#0f172a; --border:#1e293b; --text:#f8fafc; --muted:#94a3b8; --accent:#6366f1; --cyan:#06b6d4; }}
  * {{ box-sizing:border-box; font-family:'Inter', -apple-system, sans-serif; }}
  body {{ margin:0; background:var(--bg); color:var(--text); }}
  .topbar {{ display:flex; align-items:center; justify-content:space-between; padding:16px 28px; background:#0b1120; border-bottom:1px solid var(--border); }}
  .nav-links {{ display:flex; gap:10px; align-items:center; }}
  .nav-btn {{ text-decoration:none; color:#cbd5e1; background:#1e293b; padding:8px 14px; border-radius:10px; font-size:13px; font-weight:600; }}
  .nav-btn.active {{ background:var(--accent); color:#fff; }}
  .container {{ max-width:1100px; margin:28px auto; padding:0 20px; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:16px; padding:22px; margin-bottom:22px; }}
  h2, h3 {{ margin:0 0 14px; }}
  .grid-form {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:14px; }}
  label {{ display:block; font-size:11px; color:var(--muted); margin-bottom:6px; text-transform:uppercase; font-weight:600; }}
  input, select {{ width:100%; padding:10px 12px; border-radius:10px; border:1px solid #334155; background:#020617; color:#fff; font-size:13px; }}
  .btn-primary {{ background:linear-gradient(135deg, var(--accent), #4f46e5); color:#fff; border:none; padding:10px 16px; border-radius:10px; font-weight:600; cursor:pointer; }}
  .btn-sm {{ background:#1e293b; color:#e2e8f0; border:1px solid #334155; padding:6px 10px; border-radius:8px; font-size:12px; cursor:pointer; margin-left:4px; }}
  .btn-danger {{ background:rgba(239,68,68,0.15); border-color:rgba(239,68,68,0.4); color:#fca5a5; }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ padding:12px 10px; border-bottom:1px solid var(--border); text-align:left; font-size:13px; }}
  th {{ color:var(--muted); font-size:11px; text-transform:uppercase; }}
  .pill {{ padding:3px 9px; border-radius:999px; font-size:11px; font-weight:600; }}
  .pill-admin {{ background:rgba(99,102,241,0.2); color:#a5b4fc; }}
  .pill-member {{ background:rgba(6,182,212,0.18); color:#67e8f9; }}
  .pill-ok {{ background:rgba(16,185,129,0.18); color:#6ee7b7; }}
  .pill-off {{ background:rgba(239,68,68,0.18); color:#fca5a5; }}
  .banner-ok {{ background:rgba(16,185,129,0.15); border:1px solid rgba(16,185,129,0.4); color:#6ee7b7; padding:12px 16px; border-radius:12px; margin-bottom:18px; font-size:13px; }}
  .banner-err {{ background:rgba(239,68,68,0.15); border:1px solid rgba(239,68,68,0.4); color:#fca5a5; padding:12px 16px; border-radius:12px; margin-bottom:18px; font-size:13px; }}
</style>
</head>
<body>
  <div class="topbar">
    <div style="display:flex;align-items:center;gap:10px;">
      <strong style="font-size:16px;">CodeXplus Lead Engine</strong>
      <span style="color:#64748b;font-size:12px;">· Team & Access</span>
    </div>
    <div class="nav-links">
      <a href="/" class="nav-btn">Leads Dashboard</a>
      <a href="/users" class="nav-btn active">Users ({len(all_users)})</a>
      <a href="/logout" class="nav-btn">Logout ({html.escape(user['name'])})</a>
    </div>
  </div>
  <div class="container">
    {banner}
    {create_panel}
    <div class="card">
      <h3>Workspace Users</h3>
      <table>
        <thead>
          <tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Created</th><th style="text-align:right;">Actions</th></tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    <div class="card">
      <h3>Change Your Password</h3>
      <form method="post" action="/users/change-password" class="grid-form" style="max-width:560px;">
        <div>
          <label>New Password</label>
          <input type="password" name="new_password" minlength="6" placeholder="Enter new password" required/>
        </div>
        <div style="display:flex;align-items:flex-end;">
          <button type="submit" class="btn-primary">Update Password</button>
        </div>
      </form>
    </div>
  </div>
</body>
</html>"""


@app.post("/users/create")
def create_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("member"),
):
    require_admin(request)
    clean_email = email.strip().lower()
    clean_role = "admin" if role == "admin" else "member"
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (name, email, password_hash, role, is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (name.strip(), clean_email, hash_password(password), clean_role, datetime.now(timezone.utc).isoformat()),
        )
    except Exception:
        conn.close()
        return RedirectResponse(url="/users?err=Email+already+exists", status_code=302)
    conn.close()
    return RedirectResponse(url="/users?msg=User+created+successfully", status_code=302)


@app.post("/users/{target_id}/toggle")
def toggle_user(request: Request, target_id: int):
    admin = require_admin(request)
    if admin["id"] != target_id:
        conn = get_db()
        conn.execute("UPDATE users SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?", (target_id,))
        conn.close()
    return RedirectResponse(url="/users?msg=User+status+updated", status_code=302)


@app.post("/users/{target_id}/delete")
def delete_user(request: Request, target_id: int):
    admin = require_admin(request)
    if admin["id"] != target_id:
        conn = get_db()
        conn.execute("DELETE FROM users WHERE id = ?", (target_id,))
        conn.close()
    return RedirectResponse(url="/users?msg=User+removed", status_code=302)


@app.post("/users/change-password")
def change_password(request: Request, new_password: str = Form(...)):
    user = require_user(request)
    if len(new_password.strip()) < 6:
        return RedirectResponse(url="/users?err=Password+must+be+at+least+6+characters", status_code=302)
    conn = get_db()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password.strip()), user["id"]))
    conn.close()
    return RedirectResponse(url="/users?msg=Password+updated", status_code=302)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, platform: str = "all", status: str = "all", category: str = "all"):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    conn = get_db()
    query = "SELECT * FROM leads WHERE 1=1"
    params = []
    if platform != "all":
        query += " AND LOWER(platform) = ?"
        params.append(platform.lower())
    if status != "all":
        query += " AND status = ?"
        params.append(status)
    if category != "all":
        query += " AND service_category = ?"
        params.append(category)
    query += " ORDER BY score DESC, id DESC LIMIT 120"
    leads = [dict(r) for r in conn.execute(query, params).fetchall()]

    total_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    high_intent = conn.execute("SELECT COUNT(*) FROM leads WHERE score >= 8").fetchone()[0]
    contacted_count = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'contacted'").fetchone()[0]
    conn.close()

    active_provider = get_active_provider()
    ar_key, ar_base = get_agentrouter_config()
    env_ar_detected = bool(os.environ.get("AGENTROUTER_API_KEY", "").strip())

    settings_data = {
        "ai_provider": active_provider,
        "agentrouter_api_key": ar_key,
        "agentrouter_base_url": ar_base or "https://co.agentrouter.org/v1",
        "gemini_api_key_1": get_setting("gemini_api_key_1"),
        "gemini_api_key_2": get_setting("gemini_api_key_2"),
        "gemini_api_key_3": get_setting("gemini_api_key_3"),
        "gemini_api_key_4": get_setting("gemini_api_key_4"),
        "serper_api_key": get_setting("serper_api_key"),
        "agent_prompt": get_setting("agent_prompt", DEFAULT_AGENT_PROMPT),
        "agent_subreddits": get_setting("agent_subreddits", "forhire,freelance_forhire,DesignJobs,slavelabour,startups,SaaS,shopify,smallbusiness,Entrepreneur"),
        "agent_reddit_keywords": get_setting("agent_reddit_keywords", "[hiring],looking to hire,looking for a,need a,want to build,create my,build my,i will pay,budget"),
        "x_query": get_setting("x_query", BROAD_BUYER_DORK),
        "agent_min_score": get_setting("agent_min_score", "7"),
        "search_freshness": get_setting("search_freshness", "qdr:d"),
    }

    cards_html = ""
    for ld in leads:
        score = int(ld.get("score") or 0)
        score_cls = "score-hot" if score >= 8 else "score-warm"
        st = ld.get("status", "new")
        cards_html += f"""
        <div class="lead-card" id="lead-{ld['id']}">
          <div class="lead-top">
            <div class="badges">
              <span class="badge plat">{html.escape(ld.get('platform',''))}</span>
              <span class="badge cat">{html.escape(ld.get('service_category','Web/App Dev'))}</span>
              <span class="badge reg">{html.escape(ld.get('client_region','Global'))}</span>
              <span class="badge {score_cls}">Score {score}/10</span>
            </div>
            <select class="status-sel" onchange="updateStatus({ld['id']}, this.value)">
              <option value="new" {'selected' if st=='new' else ''}>New</option>
              <option value="contacted" {'selected' if st=='contacted' else ''}>Contacted</option>
              <option value="won" {'selected' if st=='won' else ''}>Won</option>
              <option value="archived" {'selected' if st=='archived' else ''}>Archived</option>
            </select>
          </div>
          <h3 class="lead-title">{html.escape(ld.get('title',''))}</h3>
          <div class="lead-summary">{html.escape(ld.get('summary',''))}</div>
          <details class="raw-box">
            <summary>View Original Post ({html.escape(ld.get('author',''))})</summary>
            <p>{html.escape(ld.get('raw_text',''))}</p>
          </details>
          <div class="reply-box">
            <div class="reply-head">
              <span>AI Outreach Pitch</span>
              <div>
                <button class="btn-mini" onclick="regenReply({ld['id']})">Regenerate</button>
                <button class="btn-mini btn-copy" onclick="copyReply({ld['id']})">Copy Pitch</button>
              </div>
            </div>
            <textarea id="reply-{ld['id']}" rows="3">{html.escape(ld.get('suggested_reply',''))}</textarea>
          </div>
          <div class="lead-foot">
            <a href="{html.escape(ld.get('url','#'))}" target="_blank" rel="noopener" class="open-link">Open Original Post &rarr;</a>
            <button class="btn-mini btn-del" onclick="deleteLead({ld['id']})">Dismiss</button>
          </div>
        </div>
        """

    if not cards_html:
        cards_html = """
        <div class="empty-state">
          <h3>No Qualified Leads Yet</h3>
          <p>Select <strong>Past 24 Hours</strong> and click <strong>Scan All Platforms</strong> to hunt live client posts across Reddit, X, LinkedIn, Threads, Facebook, Telegram &amp; Discord.</p>
        </div>
        """

    provider_badge = "AgentRouter (.env)" if (active_provider == "agentrouter" and env_ar_detected) else ("AgentRouter" if active_provider == "agentrouter" else "Gemini Pool")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>CodeXplus Lead Engine · Buyer Intelligence</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml"/>
<style>
  :root {{ --bg:#070b14; --panel:#0f172a; --panel2:#131c31; --border:#1e293b; --text:#f8fafc; --muted:#94a3b8; --accent:#6366f1; --cyan:#06b6d4; --green:#10b981; }}
  * {{ box-sizing:border-box; font-family:'Inter', -apple-system, BlinkMacSystemFont, sans-serif; }}
  body {{ margin:0; background:var(--bg); color:var(--text); }}
  .topbar {{ position:sticky; top:0; z-index:20; display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between; padding:14px 24px; background:rgba(11,17,32,0.92); backdrop-filter:blur(12px); border-bottom:1px solid var(--border); }}
  .brand {{ display:flex; align-items:center; gap:10px; }}
  .logo {{ width:34px; height:34px; border-radius:10px; background:linear-gradient(135deg, var(--accent), var(--cyan)); display:flex; align-items:center; justify-content:center; font-weight:800; font-size:14px; }}
  .active-engine {{ font-size:11px; padding:4px 10px; border-radius:999px; background:rgba(16,185,129,0.14); border:1px solid rgba(16,185,129,0.35); color:#6ee7b7; font-weight:600; }}
  .controls {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
  select, input, textarea {{ background:#020617; color:var(--text); border:1px solid #334155; border-radius:10px; padding:8px 12px; font-size:13px; }}
  .btn {{ border:none; border-radius:10px; padding:9px 14px; font-size:13px; font-weight:600; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; gap:6px; }}
  .btn-primary {{ background:linear-gradient(135deg, var(--accent), #4f46e5); color:#fff; }}
  .btn-secondary {{ background:#1e293b; color:#e2e8f0; border:1px solid #334155; }}
  .wrap {{ max-width:1360px; margin:20px auto; padding:0 20px; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:14px; margin-bottom:18px; }}
  .stat-card {{ background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:16px; }}
  .stat-label {{ font-size:11px; color:var(--muted); text-transform:uppercase; font-weight:600; }}
  .stat-val {{ font-size:24px; font-weight:800; margin-top:4px; }}
  .status-bar {{ background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:12px 16px; margin-bottom:18px; font-size:13px; color:#cbd5e1; display:flex; justify-content:space-between; align-items:center; }}
  .filters {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:18px; }}
  .chip {{ padding:6px 12px; border-radius:999px; background:var(--panel); border:1px solid var(--border); color:#cbd5e1; text-decoration:none; font-size:12px; font-weight:600; }}
  .chip.active {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(390px, 1fr)); gap:16px; }}
  .lead-card {{ background:var(--panel); border:1px solid var(--border); border-radius:16px; padding:18px; display:flex; flex-direction:column; gap:12px; transition:border-color 0.2s; }}
  .lead-card:hover {{ border-color:#4f46e5; }}
  .lead-top {{ display:flex; justify-content:space-between; align-items:center; gap:8px; }}
  .badges {{ display:flex; flex-wrap:wrap; gap:6px; }}
  .badge {{ font-size:11px; font-weight:700; padding:3px 8px; border-radius:6px; }}
  .plat {{ background:rgba(99,102,241,0.2); color:#a5b4fc; }}
  .cat {{ background:rgba(6,182,212,0.16); color:#67e8f9; }}
  .reg {{ background:#1e293b; color:#cbd5e1; }}
  .score-hot {{ background:rgba(16,185,129,0.2); color:#6ee7b7; }}
  .score-warm {{ background:rgba(245,158,11,0.2); color:#fcd34d; }}
  .lead-title {{ margin:0; font-size:15px; line-height:1.4; }}
  .lead-summary {{ font-size:13px; color:#cbd5e1; background:var(--panel2); padding:10px 12px; border-radius:10px; border-left:3px solid var(--cyan); }}
  .raw-box summary {{ cursor:pointer; font-size:12px; color:var(--muted); }}
  .raw-box p {{ font-size:12px; color:#94a3b8; white-space:pre-wrap; background:#020617; padding:10px; border-radius:8px; max-height:140px; overflow:auto; }}
  .reply-box textarea {{ width:100%; margin-top:6px; font-size:12px; line-height:1.45; }}
  .reply-head {{ display:flex; justify-content:space-between; align-items:center; font-size:11px; color:var(--muted); font-weight:600; text-transform:uppercase; }}
  .btn-mini {{ background:#1e293b; color:#e2e8f0; border:1px solid #334155; border-radius:7px; padding:4px 9px; font-size:11px; cursor:pointer; }}
  .btn-copy {{ background:rgba(99,102,241,0.2); border-color:rgba(99,102,241,0.45); color:#c7d2fe; }}
  .btn-del {{ color:#fca5a5; }}
  .lead-foot {{ display:flex; justify-content:space-between; align-items:center; margin-top:auto; padding-top:6px; border-top:1px solid var(--border); }}
  .open-link {{ color:var(--cyan); text-decoration:none; font-size:13px; font-weight:600; }}
  .empty-state {{ grid-column:1/-1; text-align:center; padding:56px 20px; background:var(--panel); border:1px dashed #334155; border-radius:16px; color:var(--muted); }}
  .modal-bg {{ display:none; position:fixed; inset:0; background:rgba(2,6,23,0.82); z-index:50; align-items:center; justify-content:center; padding:16px; }}
  .modal {{ background:var(--panel); border:1px solid var(--border); border-radius:18px; width:100%; max-width:740px; max-height:90vh; overflow:auto; padding:24px; }}
  .modal h2 {{ margin:0 0 14px; font-size:18px; }}
  .field {{ margin-bottom:14px; }}
  .field label {{ display:block; font-size:11px; font-weight:700; color:var(--muted); text-transform:uppercase; margin-bottom:6px; }}
  .field input, .field textarea, .field select {{ width:100%; }}
  .row-inline {{ display:flex; gap:8px; align-items:center; }}
  .provider-box {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:14px; }}
  .prov-opt {{ border:1px solid #334155; border-radius:12px; padding:12px; cursor:pointer; background:#020617; }}
  .prov-opt.active {{ border-color:var(--accent); background:rgba(99,102,241,0.12); }}
</style>
</head>
<body>
  <div class="topbar">
    <div class="brand">
      <div class="logo">&lt;/&gt;</div>
      <div>
        <strong style="font-size:15px;">CodeXplus Lead Engine</strong>
        <span class="active-engine" style="margin-left:8px;">AI: {html.escape(provider_badge)}</span>
      </div>
    </div>
    <div class="controls">
      <select id="freshnessSel">
        <option value="qdr:h" {'selected' if settings_data['search_freshness']=='qdr:h' else ''}>Past 1 Hour</option>
        <option value="qdr:d" {'selected' if settings_data['search_freshness']=='qdr:d' else ''}>Past 24 Hours</option>
        <option value="qdr:w" {'selected' if settings_data['search_freshness']=='qdr:w' else ''}>Past 7 Days</option>
      </select>
      <button class="btn btn-primary" onclick="triggerScan()">Scan All Platforms</button>
      <button class="btn btn-secondary" onclick="openModal()">Agent Studio &amp; Keys</button>
      <a href="/users" class="btn btn-secondary">Users</a>
      <a href="/logout" class="btn btn-secondary">Logout</a>
    </div>
  </div>

  <div class="wrap">
    <div class="stats">
      <div class="stat-card"><div class="stat-label">Total Qualified Leads</div><div class="stat-val">{total_leads}</div></div>
      <div class="stat-card"><div class="stat-label">High-Intent (8–10)</div><div class="stat-val" style="color:#6ee7b7;">{high_intent}</div></div>
      <div class="stat-card"><div class="stat-label">Contacted</div><div class="stat-val" style="color:#67e8f9;">{contacted_count}</div></div>
      <div class="stat-card"><div class="stat-label">Strict AI Provider</div><div class="stat-val" style="font-size:16px;padding-top:6px;">{html.escape(provider_badge)}</div></div>
    </div>

    <div class="status-bar">
      <span id="scanStatusText">{html.escape(get_scan_status().get('notes', 'Ready'))}</span>
      <button class="btn-mini" onclick="refreshStatus()">Refresh Status</button>
    </div>

    <div class="filters">
      <a class="chip {'active' if platform=='all' else ''}" href="/?platform=all&status={status}">All Platforms</a>
      <a class="chip {'active' if platform=='reddit' else ''}" href="/?platform=reddit&status={status}">Reddit</a>
      <a class="chip {'active' if platform=='x' else ''}" href="/?platform=x&status={status}">X</a>
      <a class="chip {'active' if platform=='linkedin' else ''}" href="/?platform=linkedin&status={status}">LinkedIn</a>
      <a class="chip {'active' if platform=='threads' else ''}" href="/?platform=threads&status={status}">Threads</a>
      <a class="chip {'active' if platform=='facebook' else ''}" href="/?platform=facebook&status={status}">Facebook</a>
      <a class="chip {'active' if platform=='telegram' else ''}" href="/?platform=telegram&status={status}">Telegram</a>
      <a class="chip {'active' if platform=='discord' else ''}" href="/?platform=discord&status={status}">Discord</a>
    </div>

    <div class="grid">{cards_html}</div>
  </div>

  <div class="modal-bg" id="studioModal">
    <div class="modal">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
        <h2>Agent Studio &amp; Strict Provider Selection</h2>
        <button class="btn-mini" onclick="closeModal()">Close</button>
      </div>

      <div class="field">
        <label>Choose Active AI Provider (No Unwanted Fallback)</label>
        <div class="provider-box">
          <div class="prov-opt {'active' if active_provider=='agentrouter' else ''}" id="opt-ar" onclick="selectProvider('agentrouter')">
            <strong>1. AgentRouter API (.env / Key)</strong>
            <div style="font-size:12px;color:#94a3b8;margin-top:4px;">{'Detected in .env' if env_ar_detected else 'Uses AGENTROUTER_API_KEY'}</div>
          </div>
          <div class="prov-opt {'active' if active_provider=='gemini' else ''}" id="opt-gem" onclick="selectProvider('gemini')">
            <strong>2. Google Gemini API Pool</strong>
            <div style="font-size:12px;color:#94a3b8;margin-top:4px;">Uses Gemini Key #1–#4</div>
          </div>
        </div>
        <input type="hidden" id="ai_provider" value="{html.escape(active_provider)}"/>
      </div>

      <div id="arSection" style="display:{'block' if active_provider=='agentrouter' else 'none'};">
        <div class="field">
          <label>AgentRouter API Key (Leave blank to keep .env key)</label>
          <div class="row-inline">
            <input type="password" id="agentrouter_api_key" value="{html.escape(settings_data['agentrouter_api_key'])}" placeholder="sk-... (loaded from .env if set)"/>
            <button class="btn-mini" onclick="testAR()">Test</button>
          </div>
        </div>
        <div class="field">
          <label>AgentRouter Base URL</label>
          <input type="text" id="agentrouter_base_url" value="{html.escape(settings_data['agentrouter_base_url'])}"/>
        </div>
      </div>

      <div id="gemSection" style="display:{'block' if active_provider=='gemini' else 'none'};">
        <div class="field">
          <label>Gemini API Key #1</label>
          <div class="row-inline">
            <input type="password" id="gemini_api_key_1" value="{html.escape(settings_data['gemini_api_key_1'])}"/>
            <button class="btn-mini" onclick="testGem(1)">Test</button>
          </div>
        </div>
        <div class="field"><label>Gemini API Key #2</label><input type="password" id="gemini_api_key_2" value="{html.escape(settings_data['gemini_api_key_2'])}"/></div>
        <div class="field"><label>Gemini API Key #3</label><input type="password" id="gemini_api_key_3" value="{html.escape(settings_data['gemini_api_key_3'])}"/></div>
        <div class="field"><label>Gemini API Key #4</label><input type="password" id="gemini_api_key_4" value="{html.escape(settings_data['gemini_api_key_4'])}"/></div>
      </div>

      <div id="testResult" style="font-size:12px;color:#67e8f9;margin-bottom:12px;"></div>

      <div class="field">
        <label>Serper.dev Google Search API Key (X, LinkedIn, Threads, Facebook, Telegram, Discord)</label>
        <input type="password" id="serper_api_key" value="{html.escape(settings_data['serper_api_key'])}"/>
      </div>

      <div class="field">
        <label>Buyer Qualification Prompt</label>
        <textarea id="agent_prompt" rows="5">{html.escape(settings_data['agent_prompt'])}</textarea>
      </div>

      <div class="field">
        <label>Subreddits (comma-separated)</label>
        <input type="text" id="agent_subreddits" value="{html.escape(settings_data['agent_subreddits'])}"/>
      </div>

      <div class="field">
        <label>Minimum Lead Score (1–10)</label>
        <input type="number" id="agent_min_score" min="1" max="10" value="{html.escape(settings_data['agent_min_score'])}"/>
      </div>

      <div style="display:flex;flex-wrap:wrap;gap:10px;justify-content:space-between;margin-top:18px;">
        <button class="btn btn-secondary" onclick="resetSeen()">Reset Seen Cache</button>
        <button class="btn btn-primary" onclick="saveSettings()">Save All Settings</button>
      </div>
    </div>
  </div>

<script>
  function openModal() {{ document.getElementById('studioModal').style.display = 'flex'; }}
  function closeModal() {{ document.getElementById('studioModal').style.display = 'none'; }}
  function selectProvider(p) {{
    document.getElementById('ai_provider').value = p;
    document.getElementById('opt-ar').classList.toggle('active', p === 'agentrouter');
    document.getElementById('opt-gem').classList.toggle('active', p === 'gemini');
    document.getElementById('arSection').style.display = p === 'agentrouter' ? 'block' : 'none';
    document.getElementById('gemSection').style.display = p === 'gemini' ? 'block' : 'none';
  }}
  async function testAR() {{
    const res = await fetch('/api/test-provider', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{ provider:'agentrouter', api_key: document.getElementById('agentrouter_api_key').value, base_url: document.getElementById('agentrouter_base_url').value }})
    }});
    const d = await res.json();
    document.getElementById('testResult').innerText = d.detail;
  }}
  async function testGem(idx) {{
    const res = await fetch('/api/test-provider', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{ provider:'gemini', api_key: document.getElementById('gemini_api_key_' + idx).value }})
    }});
    const d = await res.json();
    document.getElementById('testResult').innerText = d.detail;
  }}
  async function saveSettings() {{
    const payload = {{
      ai_provider: document.getElementById('ai_provider').value,
      agentrouter_api_key: document.getElementById('agentrouter_api_key').value,
      agentrouter_base_url: document.getElementById('agentrouter_base_url').value,
      gemini_api_key_1: document.getElementById('gemini_api_key_1').value,
      gemini_api_key_2: document.getElementById('gemini_api_key_2').value,
      gemini_api_key_3: document.getElementById('gemini_api_key_3').value,
      gemini_api_key_4: document.getElementById('gemini_api_key_4').value,
      serper_api_key: document.getElementById('serper_api_key').value,
      agent_prompt: document.getElementById('agent_prompt').value,
      agent_subreddits: document.getElementById('agent_subreddits').value,
      agent_min_score: document.getElementById('agent_min_score').value,
      search_freshness: document.getElementById('freshnessSel').value
    }};
    await fetch('/api/settings', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(payload) }});
    location.reload();
  }}
  async function resetSeen() {{
    const res = await fetch('/api/reset-seen', {{ method:'POST' }});
    const d = await res.json();
    document.getElementById('testResult').innerText = 'Cleared ' + d.cleared + ' seen items.';
  }}
  async function triggerScan() {{
    document.getElementById('scanStatusText').innerText = 'Scanning platforms... please wait...';
    const freshness = document.getElementById('freshnessSel').value;
    const res = await fetch('/api/sync', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{ freshness }})
    }});
    const d = await res.json();
    document.getElementById('scanStatusText').innerText = d.notes || 'Done';
    if (d.saved > 0) setTimeout(() => location.reload(), 900);
  }}
  async function refreshStatus() {{
    const res = await fetch('/api/status');
    const d = await res.json();
    document.getElementById('scanStatusText').innerText = d.notes || 'Ready';
  }}
  async function updateStatus(id, status) {{
    await fetch('/api/leads/' + id + '/status', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{ status }}) }});
  }}
  async function deleteLead(id) {{
    await fetch('/api/leads/' + id, {{ method:'DELETE' }});
    document.getElementById('lead-' + id).remove();
  }}
  async function regenReply(id) {{
    const res = await fetch('/api/leads/' + id + '/regenerate', {{ method:'POST' }});
    const d = await res.json();
    if (d.reply) document.getElementById('reply-' + id).value = d.reply;
  }}
  function copyReply(id) {{
    const el = document.getElementById('reply-' + id);
    el.select();
    navigator.clipboard.writeText(el.value);
  }}
</script>
</body>
</html>"""


@app.get("/api/status")
def api_status(request: Request):
    require_user(request)
    return get_scan_status()


@app.post("/api/settings")
async def api_save_settings(request: Request):
    require_user(request)
    data = await request.json()
    allowed = [
        "ai_provider",
        "agentrouter_api_key",
        "agentrouter_base_url",
        "gemini_api_key_1",
        "gemini_api_key_2",
        "gemini_api_key_3",
        "gemini_api_key_4",
        "serper_api_key",
        "agent_prompt",
        "agent_subreddits",
        "agent_min_score",
        "search_freshness",
    ]
    for k in allowed:
        if k in data and data[k] is not None:
            set_setting(k, str(data[k]).strip())
    return {"ok": True}


@app.post("/api/test-provider")
async def api_test_provider(request: Request):
    require_user(request)
    data = await request.json()
    prov = data.get("provider", "agentrouter")
    if prov == "agentrouter":
        key = data.get("api_key", "") or get_agentrouter_config()[0]
        base = data.get("base_url", "") or get_agentrouter_config()[1]
        return test_agentrouter_key(key, base)
    return test_single_gemini_key(data.get("api_key", ""))


@app.post("/api/reset-seen")
def api_reset_seen(request: Request):
    require_user(request)
    return {"ok": True, "cleared": clear_seen_cache()}


@app.post("/api/sync")
async def api_sync(request: Request):
    require_user(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    return run_sync(freshness=body.get("freshness"), platform=body.get("platform", "all"))


@app.post("/api/leads/{lead_id}/status")
async def api_lead_status(request: Request, lead_id: int):
    require_user(request)
    data = await request.json()
    conn = get_db()
    conn.execute("UPDATE leads SET status = ? WHERE id = ?", (data.get("status", "new"), lead_id))
    conn.close()
    return {"ok": True}


@app.delete("/api/leads/{lead_id}")
def api_lead_delete(request: Request, lead_id: int):
    require_user(request)
    conn = get_db()
    conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    conn.close()
    return {"ok": True}


@app.post("/api/leads/{lead_id}/regenerate")
def api_lead_regen(request: Request, lead_id: int):
    require_user(request)
    conn = get_db()
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Lead not found")
    reply = regenerate_lead_reply(dict(row))
    conn.execute("UPDATE leads SET suggested_reply = ? WHERE id = ?", (reply, lead_id))
    conn.close()
    return {"ok": True, "reply": reply}
