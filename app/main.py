import os
import threading
from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "codexplus2026")


def _render_login_html(error: str = "") -> str:
    err_block = f'<p style="color:#f87171;margin-bottom:12px;font-size:14px;">{error}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CodeXplus Lead Engine — Login</title>
  <style>
    body {{ background:#0b0f19; color:#e2e8f0; font-family:system-ui,-apple-system,sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
    .card {{ background:#111827; border:1px solid #1f2937; border-radius:12px; padding:32px; width:100%; max-width:360px; box-shadow:0 10px 30px rgba(0,0,0,0.4); }}
    h1 {{ font-size:20px; margin:0 0 8px; color:#f8fafc; }}
    p.sub {{ color:#94a3b8; font-size:13px; margin:0 0 20px; }}
    input {{ width:100%; box-sizing:border-box; padding:10px 12px; border-radius:8px; border:1px solid #374151; background:#0f172a; color:#f8fafc; margin-bottom:14px; font-size:14px; }}
    button {{ width:100%; padding:10px; border:none; border-radius:8px; background:#10b981; color:#052e16; font-weight:700; font-size:14px; cursor:pointer; }}
    button:hover {{ background:#34d399; }}
  </style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <h1>CodeXplus Lead Engine</h1>
    <p class="sub">Enter admin password to access lead scanner</p>
    {err_block}
    <input type="password" name="password" placeholder="Admin password" required autofocus />
    <button type="submit">Sign In</button>
  </form>
</body>
</html>"""


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CodeXplus Lead Engine</title>
  <style>
    :root { --bg:#090d16; --card:#111827; --border:#1e293b; --text:#e2e8f0; --muted:#94a3b8; --green:#10b981; --indigo:#6366f1; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family:system-ui,-apple-system,sans-serif; padding:20px; }
    .wrap { max-width:1240px; margin:0 auto; }
    .topbar { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:12px; margin-bottom:16px; }
    .title { font-size:20px; font-weight:700; color:#f8fafc; }
    .btn { padding:8px 14px; border-radius:8px; border:1px solid #334155; background:#1e293b; color:#f8fafc; font-weight:600; font-size:13px; cursor:pointer; }
    .btn:hover { background:#334155; }
    .btn-green { background:var(--green); color:#052e16; border-color:var(--green); }
    .btn-green:hover { background:#34d399; }
    .btn-indigo { background:var(--indigo); color:#fff; border-color:var(--indigo); }
    .btn-red { background:#7f1d1d; color:#fecaca; border-color:#991b1b; }
    .panel { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; margin-bottom:16px; }
    .banner { background:#0f172a; border:1px solid #334155; border-radius:10px; padding:12px 16px; margin-bottom:16px; font-size:13px; color:#cbd5e1; display:flex; justify-content:space-between; align-items:center; }
    textarea, input, select { width:100%; background:#0b1120; color:#f8fafc; border:1px solid #334155; border-radius:8px; padding:9px 11px; font-size:13px; font-family:inherit; }
    .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:16px; }
    .stat-card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:14px 16px; }
    .stat-label { font-size:12px; color:var(--muted); margin-bottom:6px; }
    .stat-val { font-size:24px; font-weight:700; }
    .filters { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px; align-items:center; }
    .pill { padding:6px 12px; border-radius:8px; border:1px solid #334155; background:#111827; color:#cbd5e1; font-size:12px; cursor:pointer; }
    .pill.active { background:var(--indigo); border-color:var(--indigo); color:#fff; font-weight:600; }
    .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(460px,1fr)); gap:14px; }
    .lead-card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; display:flex; flex-direction:column; gap:10px; }
    .badges { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
    .badge { padding:3px 8px; border-radius:6px; font-size:11px; font-weight:700; background:#1e293b; color:#cbd5e1; }
    .badge-score { background:#065f46; color:#6ee7b7; }
    .raw-box { background:#0b1120; border:1px solid #1e293b; border-radius:8px; padding:10px; font-size:12px; color:#94a3b8; white-space:pre-wrap; max-height:110px; overflow-y:auto; }
    .modal { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.75); z-index:50; overflow-y:auto; padding:24px; }
    .modal-inner { max-width:720px; margin:20px auto; background:#111827; border:1px solid #334155; border-radius:14px; padding:22px; }
    .row2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px; }
    label { display:block; font-size:12px; color:#94a3b8; margin-bottom:4px; }
  </style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="title">CodeXplus Lead Engine <span style="font-size:12px;color:#10b981;font-weight:600;">· Gemini 4 Pool</span></div>
    <div style="display:flex;gap:8px;">
      <button class="btn" onclick="openStudio()">Agent Studio & Keys</button>
      <form method="post" action="/logout" style="margin:0;"><button class="btn" type="submit">Logout</button></form>
    </div>
  </div>

  <div class="banner" id="statusBanner">
    <span id="statusText">Ready</span>
    <button class="btn" style="padding:4px 10px;font-size:12px;" onclick="loadAll()">Refresh</button>
  </div>

  <div class="panel">
    <div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;">
      <div>
        <strong style="font-size:14px;">Active Lead Hunter Agent Prompt</strong>
        <div style="font-size:12px;color:#94a3b8;">Scans Reddit + X, LinkedIn, Threads, Facebook, Telegram, Discord & Twist concurrently.</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;">
        <select id="freshnessSel" style="width:auto;">
          <option value="qdr:h">Past 1 Hour (First-Mover)</option>
          <option value="qdr:d">Past 24 Hours</option>
          <option value="qdr:w">Past 7 Days</option>
        </select>
        <button class="btn btn-indigo" onclick="updateDorks()">Update Dorks</button>
        <button class="btn btn-green" onclick="triggerScan()">Scan All Platforms</button>
      </div>
    </div>
    <textarea id="agentPromptBox" rows="4"></textarea>
  </div>

  <div class="stats">
    <div class="stat-card"><div class="stat-label">Saved Qualified Leads</div><div class="stat-val" id="statTotal">0</div></div>
    <div class="stat-card"><div class="stat-label">New Unreviewed</div><div class="stat-val" style="color:#818cf8;" id="statNew">0</div></div>
    <div class="stat-card"><div class="stat-label">High Intent (Score 7+)</div><div class="stat-val" style="color:#34d399;" id="statHigh">0</div></div>
    <div class="stat-card"><div class="stat-label">Replied / Pitched</div><div class="stat-val" style="color:#fbbf24;" id="statReplied">0</div></div>
  </div>

  <div class="filters" id="platformPills">
    <button class="pill active" data-plat="all" onclick="setPlat('all', this)">All Channels</button>
    <button class="pill" data-plat="Reddit" onclick="setPlat('Reddit', this)">Reddit</button>
    <button class="pill" data-plat="X" onclick="setPlat('X', this)">X / Twitter</button>
    <button class="pill" data-plat="LinkedIn" onclick="setPlat('LinkedIn', this)">LinkedIn</button>
    <button class="pill" data-plat="Threads" onclick="setPlat('Threads', this)">Threads</button>
    <button class="pill" data-plat="Facebook" onclick="setPlat('Facebook', this)">Facebook</button>
    <button class="pill" data-plat="Telegram" onclick="setPlat('Telegram', this)">Telegram</button>
    <button class="pill" data-plat="Discord" onclick="setPlat('Discord', this)">Discord</button>
    <button class="pill" data-plat="Twist" onclick="setPlat('Twist', this)">Twist</button>
  </div>

  <div class="grid" id="leadsGrid"></div>
</div>

<!-- Agent Studio & Keys Modal -->
<div class="modal" id="studioModal">
  <div class="modal-inner">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
      <strong style="font-size:16px;">Agent Studio & API Keys (Gemini 4 Pool)</strong>
      <button class="btn" onclick="closeStudio()">Close</button>
    </div>

    <div style="margin-bottom:10px;">
      <label>Serper.dev Google Search API Key (for X, LinkedIn, Threads, Facebook, Telegram, Discord, Twist)</label>
      <input id="serperKey" type="password" placeholder="Serper API Key" />
    </div>

    <div class="row2">
      <div>
        <label>Gemini API Key #1</label>
        <div style="display:flex;gap:6px;"><input id="gemKey1" type="password" /><button class="btn" onclick="testGem(1)">Test</button></div>
      </div>
      <div>
        <label>Gemini API Key #2</label>
        <div style="display:flex;gap:6px;"><input id="gemKey2" type="password" /><button class="btn" onclick="testGem(2)">Test</button></div>
      </div>
      <div>
        <label>Gemini API Key #3</label>
        <div style="display:flex;gap:6px;"><input id="gemKey3" type="password" /><button class="btn" onclick="testGem(3)">Test</button></div>
      </div>
      <div>
        <label>Gemini API Key #4</label>
        <div style="display:flex;gap:6px;"><input id="gemKey4" type="password" /><button class="btn" onclick="testGem(4)">Test</button></div>
      </div>
    </div>
    <div id="keyTestResult" style="font-size:12px;color:#34d399;margin-bottom:12px;"></div>

    <div class="row2">
      <div>
        <label>Minimum AI Score to Save (1-10)</label>
        <input id="minScore" type="number" min="1" max="10" value="7" />
      </div>
      <div>
        <label>Target Subreddits (comma-separated)</label>
        <input id="subredditsBox" type="text" />
      </div>
    </div>

    <div style="margin-bottom:10px;">
      <label>Reddit Trigger Keywords</label>
      <input id="keywordsBox" type="text" />
    </div>

    <div style="margin-bottom:14px;">
      <label>Social Search Boolean Dork</label>
      <textarea id="dorkBox" rows="2"></textarea>
    </div>

    <div style="display:flex;flex-wrap:wrap;gap:10px;justify-content:space-between;">
      <button class="btn btn-red" onclick="resetSeen()">Reset Seen Cache</button>
      <button class="btn btn-green" onclick="saveSettings()">Save All Settings</button>
    </div>
  </div>
</div>

<script>
let allLeads = [];
let activePlat = 'all';
let pollTimer = null;

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function loadAll() {
  const [leadsRes, cfgRes, statusRes] = await Promise.all([
    fetch('/api/leads'),
    fetch('/api/settings'),
    fetch('/api/scan/status')
  ]);
  if (leadsRes.ok) {
    const d = await leadsRes.json();
    allLeads = d.leads || [];
    renderLeads();
  }
  if (cfgRes.ok) {
    const c = await cfgRes.json();
    document.getElementById('agentPromptBox').value = c.agent_prompt || '';
    document.getElementById('freshnessSel').value = c.search_freshness || 'qdr:h';
    document.getElementById('serperKey').value = c.serper_api_key || '';
    document.getElementById('gemKey1').value = c.gemini_api_key_1 || '';
    document.getElementById('gemKey2').value = c.gemini_api_key_2 || '';
    document.getElementById('gemKey3').value = c.gemini_api_key_3 || '';
    document.getElementById('gemKey4').value = c.gemini_api_key_4 || '';
    document.getElementById('minScore').value = c.agent_min_score || '7';
    document.getElementById('subredditsBox').value = c.agent_subreddits || '';
    document.getElementById('keywordsBox').value = c.agent_reddit_keywords || '';
    document.getElementById('dorkBox').value = c.x_query || '';
  }
  if (statusRes.ok) {
    const st = await statusRes.json();
    document.getElementById('statusText').textContent = st.notes || 'Ready';
  }
}

function setPlat(plat, el) {
  activePlat = plat;
  document.querySelectorAll('#platformPills .pill').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  renderLeads();
}

function renderLeads() {
  document.getElementById('statTotal').textContent = allLeads.length;
  document.getElementById('statNew').textContent = allLeads.filter(l => l.status === 'new').length;
  document.getElementById('statHigh').textContent = allLeads.filter(l => (l.score || 0) >= 7).length;
  document.getElementById('statReplied').textContent = allLeads.filter(l => l.status === 'replied').length;

  const filtered = activePlat === 'all' ? allLeads : allLeads.filter(l => (l.platform || '').toLowerCase() === activePlat.toLowerCase());
  const grid = document.getElementById('leadsGrid');
  if (!filtered.length) {
    grid.innerHTML = '<div class="panel" style="grid-column:1/-1;color:#94a3b8;">No saved leads match this filter yet. Click <strong>Scan All Platforms</strong> above.</div>';
    return;
  }
  grid.innerHTML = filtered.map(l => `
    <div class="lead-card">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <div class="badges">
          <span class="badge">${esc(l.platform)}</span>
          <span class="badge badge-score">Score ${esc(l.score)}/10</span>
          <span class="badge">${esc(l.service_category)}</span>
          <span class="badge">${esc(l.client_region)}</span>
        </div>
        <a href="${esc(l.url)}" target="_blank" style="color:#60a5fa;font-size:12px;text-decoration:none;">Original Post ↗</a>
      </div>
      <div style="font-weight:700;font-size:15px;">${esc(l.title)}</div>
      <div style="font-size:13px;color:#34d399;">${esc(l.summary)}</div>
      <div class="raw-box">${esc(l.raw_text)}</div>
      <div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
          <span style="font-size:12px;color:#94a3b8;">AI Client Pitch</span>
          <button class="btn" style="padding:3px 8px;font-size:11px;" onclick="regenReply(${l.id})">Regenerate</button>
        </div>
        <textarea id="reply_${l.id}" rows="3">${esc(l.suggested_reply)}</textarea>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end;">
        <button class="btn" onclick="copyReply(${l.id})">Copy Pitch</button>
        <button class="btn btn-green" onclick="markReplied(${l.id})">${l.status === 'replied' ? 'Replied ✓' : 'Mark Replied'}</button>
      </div>
    </div>
  `).join('');
}

async function triggerScan() {
  document.getElementById('statusText').textContent = 'Starting scan across all platforms on Gemini 4 pool...';
  await fetch('/api/sync', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      prompt: document.getElementById('agentPromptBox').value,
      freshness: document.getElementById('freshnessSel').value,
      platform: activePlat
    })
  });
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const r = await fetch('/api/scan/status');
    if (!r.ok) return;
    const st = await r.json();
    document.getElementById('statusText').textContent = st.notes || 'Scanning...';
    if (!st.running) {
      clearInterval(pollTimer);
      loadAll();
    }
  }, 2000);
}

async function updateDorks() {
  document.getElementById('statusText').textContent = 'Compiling search dorks via Gemini 4...';
  const r = await fetch('/api/generate-dorks', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt: document.getElementById('agentPromptBox').value})
  });
  const d = await r.json();
  document.getElementById('statusText').textContent = d.ok ? 'Search dorks updated!' : 'Failed to update dorks';
  loadAll();
}

function openStudio() { document.getElementById('studioModal').style.display = 'block'; }
function closeStudio() { document.getElementById('studioModal').style.display = 'none'; }

async function saveSettings() {
  await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      agent_prompt: document.getElementById('agentPromptBox').value,
      search_freshness: document.getElementById('freshnessSel').value,
      serper_api_key: document.getElementById('serperKey').value,
      gemini_api_key_1: document.getElementById('gemKey1').value,
      gemini_api_key_2: document.getElementById('gemKey2').value,
      gemini_api_key_3: document.getElementById('gemKey3').value,
      gemini_api_key_4: document.getElementById('gemKey4').value,
      agent_min_score: document.getElementById('minScore').value,
      agent_subreddits: document.getElementById('subredditsBox').value,
      agent_reddit_keywords: document.getElementById('keywordsBox').value,
      x_query: document.getElementById('dorkBox').value
    })
  });
  document.getElementById('keyTestResult').textContent = 'All settings saved!';
  loadAll();
}

async function testGem(idx) {
  const val = document.getElementById('gemKey' + idx).value;
  document.getElementById('keyTestResult').textContent = 'Testing Gemini Key #' + idx + '...';
  const r = await fetch('/api/test-gemini', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: val})
  });
  const d = await r.json();
  document.getElementById('keyTestResult').textContent = 'Key #' + idx + ': ' + d.detail;
}

async function resetSeen() {
  const r = await fetch('/api/reset-seen', {method: 'POST'});
  const d = await r.json();
  document.getElementById('keyTestResult').textContent = 'Cleared ' + (d.cleared || 0) + ' seen posts from cache.';
}

async function regenReply(id) {
  const r = await fetch('/api/leads/' + id + '/regenerate', {method: 'POST'});
  if (r.ok) {
    const d = await r.json();
    document.getElementById('reply_' + id).value = d.reply;
  }
}

function copyReply(id) {
  const txt = document.getElementById('reply_' + id).value;
  navigator.clipboard.writeText(txt);
}

async function markReplied(id) {
  await fetch('/api/leads/' + id, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: 'replied', suggested_reply: document.getElementById('reply_' + id).value})
  });
  loadAll();
}

loadAll();
</script>
</body>
</html>"""


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
        return HTMLResponse(content=_render_login_html())
    return HTMLResponse(content=DASHBOARD_HTML)


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("cx_auth", ADMIN_PASSWORD, httponly=True, max_age=86400 * 30)
        return resp
    return HTMLResponse(content=_render_login_html("Invalid password"), status_code=401)


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


@app.get("/api/settings")
def api_get_settings(request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {
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
    }


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