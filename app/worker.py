import os
import re
import json
import time
import sqlite3
import hashlib
from datetime import datetime, timezone
import httpx

DB_PATH = os.environ.get("DB_PATH", "/app/data/leads.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

DEFAULT_AGENT_PROMPT = """Hunt for REAL CLIENTS, startup founders, and business owners actively looking to hire or place a paid order for:
- Web & Mobile Application Development (Next.js, React, Node.js, Python, Full-Stack SaaS, Shopify/WordPress)
- Custom Software, AI Automation & API Integrations
- Graphic Design, Branding & Digital Art
- Social Media Marketing, SEO & GEO (Generative Engine Optimization)
- Custom Arduino Hardware & Embedded Software Prototyping

STRICT EXCLUSIONS (SCORE 1-2 AND REJECT):
- Any freelancer, developer, designer, marketer, or agency advertising their own services ("for hire", "portfolio", "I will build", "DM me").
- Unpaid internships, equity-only cofounder posts, spam, or irrelevant non-tech jobs."""


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT UNIQUE,
            platform TEXT,
            author TEXT,
            title TEXT,
            raw_text TEXT,
            url TEXT,
            score INTEGER DEFAULT 0,
            summary TEXT,
            suggested_reply TEXT,
            status TEXT DEFAULT 'new',
            notes TEXT DEFAULT '',
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()
    return conn


def get_setting(key: str, default: str = "") -> str:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    if row and row["value"] is not None and str(row["value"]).strip() != "":
        return str(row["value"]).strip()
    env_val = os.environ.get(key.upper(), "")
    if env_val and env_val.strip():
        return env_val.strip()
    return default


def set_setting(key: str, value: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_gemini_key_pool() -> list[tuple[str, str]]:
    """Collect up to 4 configured Gemini API keys (DB settings override .env)."""
    pool = []
    seen = set()
    for idx in range(1, 5):
        k_name = f"gemini_api_key_{idx}"
        val = get_setting(k_name)
        if val and val not in seen:
            pool.append((f"Key #{idx}", val))
            seen.add(val)
    legacy = get_setting("gemini_api_key")
    if legacy and legacy not in seen:
        pool.append(("Legacy Key", legacy))
        seen.add(legacy)
    return pool


def call_gemini(prompt: str, override_keys: list[tuple[str, str]] | None = None) -> str:
    """
    Rotates across high-quota Flash-Lite models (gemini-3.5-flash-lite -> gemini-3.1-flash-lite -> gemini-3.8-flash)
    and automatically fails over across all configured keys (Key #1 -> #2 -> #3 -> #4).
    """
    keys = override_keys if override_keys is not None else get_gemini_key_pool()
    if not keys:
        raise RuntimeError("No Gemini API keys configured. Add Key #1-#4 in Admin Panel or .env.")

    models = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]
    errors = []

    for key_label, api_key in keys:
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            try:
                with httpx.Client(timeout=25.0) as client:
                    resp = client.post(
                        url,
                        json={"contents": [{"parts": [{"text": prompt}]}]},
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        return candidates[0]["content"]["parts"][0]["text"].strip()
                    errors.append(f"{key_label} ({model}): Empty response")
                    continue

                err_msg = resp.text[:140].replace("\n", " ")
                errors.append(f"{key_label} ({model}) HTTP {resp.status_code}: {err_msg}")
                if resp.status_code in (400, 401, 403):
                    break
            except Exception as exc:
                errors.append(f"{key_label} ({model}): {exc}")
                continue

    raise RuntimeError("All Gemini keys failed -> " + " | ".join(errors[:4]))


def test_single_gemini_key(api_key: str) -> dict:
    if not api_key or not api_key.strip():
        return {"ok": False, "detail": "Empty key"}
    try:
        out = call_gemini("Reply with the single word: OK", [("Test Key", api_key.strip())])
        return {"ok": True, "detail": f"Active ({out[:20]})"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:180]}


def generate_queries_from_prompt(agent_prompt: str) -> dict:
    """Uses Gemini to compile a plain-English agent prompt into Reddit & Serper search dorks."""
    compiler_prompt = f"""You are a search query engineer for a B2B/freelance client lead scraper.
Convert the user's Lead Hunter Agent Prompt into optimal search parameters.

USER AGENT PROMPT:
{agent_prompt}

Return ONLY valid JSON with these exact keys:
{{
  "subreddits": "comma-separated relevant subreddits without r/, e.g. forhire,freelance_forhire,DesignJobs,startups,SaaS,arduino",
  "reddit_keywords": "comma-separated lowercase trigger phrases to require in Reddit titles, e.g. [hiring],looking to hire,need a developer,need an app,budget",
  "serper_dork": "Google search boolean query without site: prefix to find buyers/clients with budgets, e.g. (\\"looking to hire\\" OR \\"need a developer\\" OR \\"need a website\\") (\\"budget\\" OR \\"$\\") -\\"for hire\\""
}}"""
    raw = call_gemini(compiler_prompt)
    cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    parsed = json.loads(cleaned)
    return {
        "subreddits": str(parsed.get("subreddits", "forhire,freelance_forhire,DesignJobs,slavelabour,hiring,arduino")),
        "reddit_keywords": str(parsed.get("reddit_keywords", "[hiring],looking to hire,need a,looking for a")),
        "serper_dork": str(parsed.get("serper_dork", '("looking to hire" OR "need a developer" OR "need a website" OR "need an app") ("budget" OR "$") -"for hire"')),
    }


def score_and_draft(lead: dict, agent_prompt: str) -> tuple[int, str, str]:
    prompt = f"""You are the Lead Qualification & Outreach Agent for CodeXplus (https://codexplus.site).

FOLLOW THIS ACTIVE AGENT MISSION & FILTERING RUBRIC STRICTLY:
{agent_prompt}

CANDIDATE LEAD TO EVALUATE:
Platform: {lead['platform']}
Title: {lead['title']}
Text: {lead['raw_text']}

SCORING RULES:
- Score 1 to 3 if the post violates the exclusions above, is a freelancer/agency promoting themselves ("for hire"), or does NOT match the Agent Mission.
- Score 7 to 10 ONLY if it is a genuine client/buyer matching the Agent Mission. Give 9-10 if a clear budget ($) or urgent timeline is stated.
- Write a concise, personalized, high-converting outreach reply from CodeXplus (https://codexplus.site) tailored directly to their post.

Respond ONLY in valid JSON:
{{"score": 8, "summary": "1-sentence summary of what the client needs and budget if stated", "reply": "Tailored outreach message mentioning https://codexplus.site"}}"""

    raw = call_gemini(prompt)
    cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    parsed = json.loads(cleaned)
    score = int(parsed.get("score", 1))
    summary = str(parsed.get("summary", lead["title"]))
    reply = str(parsed.get("reply", ""))
    return score, summary, reply


def regenerate_lead_reply(lead: dict) -> str:
    agent_prompt = get_setting("agent_prompt", DEFAULT_AGENT_PROMPT)
    prompt = f"""Write a fresh, concise, high-converting client outreach message from CodeXplus (https://codexplus.site) for this client lead.

ACTIVE AGENT CONTEXT:
{agent_prompt}

Platform: {lead.get('platform', '')}
Title: {lead.get('title', '')}
Post: {lead.get('raw_text', '')}

Keep it direct, friendly, under 80 words, focused on solving their exact project, and include https://codexplus.site naturally. Return only the reply text."""
    return call_gemini(prompt)


def fetch_arctic_shift_reddit() -> list[dict]:
    subs_raw = get_setting("agent_subreddits", "forhire,freelance_forhire,DesignJobs,slavelabour,hiring,arduino")
    subreddits = [s.strip().replace("r/", "") for s in subs_raw.split(",") if s.strip()]
    if not subreddits:
        subreddits = ["forhire", "freelance_forhire", "DesignJobs", "hiring"]

    kw_raw = get_setting("agent_reddit_keywords", "[hiring],looking to hire,need a,looking for a")
    keywords = [k.strip().lower() for k in kw_raw.split(",") if k.strip()]

    items = []
    now_ts = int(time.time())
    after_ts = now_ts - 10800  # Last 3 hours

    with httpx.Client(timeout=15.0) as client:
        for sub in subreddits[:8]:
            try:
                resp = client.get(
                    "https://arctic-shift.photon-reddit.com/api/posts/search",
                    params={"subreddit": sub, "after": after_ts, "limit": 20, "sort": "desc"},
                )
                if resp.status_code != 200:
                    continue
                for post in resp.json().get("data", []):
                    title = post.get("title", "")
                    body = post.get("selftext", "")
                    num_comments = post.get("num_comments", 0)
                    if num_comments > 6:
                        continue
                    lower_title = title.lower()
                    if "[for hire]" in lower_title or "for hire" in lower_title:
                        continue
                    if keywords and not any(kw in lower_title for kw in keywords):
                        continue
                    post_id = post.get("id", "")
                    permalink = post.get("permalink", f"/r/{sub}/comments/{post_id}")
                    items.append(
                        {
                            "external_id": f"reddit_{post_id}",
                            "platform": "Reddit",
                            "author": post.get("author", "unknown"),
                            "title": title,
                            "raw_text": f"{title}\n{body}"[:1200],
                            "url": f"https://www.reddit.com{permalink}",
                        }
                    )
            except Exception:
                continue
    return items


def fetch_serper_channels(serper_key: str) -> list[dict]:
    if not serper_key:
        return []
    base_dork = get_setting(
        "x_query",
        '("looking to hire" OR "need a developer" OR "need a website" OR "need an app" OR "graphic designer" OR "SEO") ("budget" OR "$") -"for hire"',
    )
    queries = [
        ("X", f"(site:x.com OR site:twitter.com) {base_dork}"),
        ("Threads", f"site:threads.net {base_dork}"),
        ("Facebook", f"site:facebook.com {base_dork}"),
        ("Telegram", f"site:t.me {base_dork}"),
        ("Discord", f"(site:discord.com OR site:discord.gg) {base_dork}"),
        ("Twist", f"site:twist.com {base_dork}"),
        ("LinkedIn", f'site:linkedin.com/posts {base_dork} -"open to work"'),
    ]
    items = []
    with httpx.Client(timeout=15.0) as client:
        for platform, q in queries:
            try:
                resp = client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                    json={"q": q, "num": 5, "tbs": "qdr:d"},
                )
                if resp.status_code != 200:
                    continue
                for res in resp.json().get("organic", []):
                    link = res.get("link", "")
                    title = res.get("title", "")
                    snippet = res.get("snippet", "")
                    ext_id = f"{platform.lower()}_{hashlib.md5(link.encode()).hexdigest()[:12]}"
                    items.append(
                        {
                            "external_id": ext_id,
                            "platform": platform,
                            "author": platform,
                            "title": title,
                            "raw_text": f"{title}\n{snippet}"[:1200],
                            "url": link,
                        }
                    )
            except Exception:
                continue
    return items


def run_sync(custom_prompt: str | None = None) -> dict:
    serper_key = get_setting("serper_api_key")
    key_pool = get_gemini_key_pool()
    agent_prompt = (custom_prompt or "").strip() or get_setting("agent_prompt", DEFAULT_AGENT_PROMPT)
    try:
        min_keep_score = int(get_setting("agent_min_score", "7"))
    except ValueError:
        min_keep_score = 7

    candidates = fetch_arctic_shift_reddit() + fetch_serper_channels(serper_key)
    conn = get_db()
    cur = conn.cursor()

    saved = 0
    rejected = 0
    notes = []

    for item in candidates:
        cur.execute("SELECT id FROM leads WHERE external_id = ?", (item["external_id"],))
        if cur.fetchone():
            continue

        score = 5
        summary = item["title"]
        reply = ""

        if key_pool:
            try:
                score, summary, reply = score_and_draft(item, agent_prompt)
                time.sleep(0.35)
                if score < min_keep_score:
                    rejected += 1
                    continue
            except Exception as exc:
                notes.append(f"Gemini ({item['external_id']}): {exc}")
        else:
            notes.append("No Gemini API keys configured.")

        cur.execute(
            """
            INSERT INTO leads (external_id, platform, author, title, raw_text, url, score, summary, suggested_reply, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                item["external_id"],
                item["platform"],
                item["author"],
                item["title"],
                item["raw_text"],
                item["url"],
                score,
                summary,
                reply,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        saved += 1

    conn.commit()
    conn.close()
    return {
        "fetched": len(candidates),
        "saved": saved,
        "rejected": rejected,
        "notes": "; ".join(notes[:3]),
    }