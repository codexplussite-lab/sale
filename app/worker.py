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
                # Only skip remaining models on this key if the key itself is invalid/unauthorized
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


def score_and_draft(lead: dict) -> tuple[int, str, str]:
    prompt = f"""You are a lead qualification assistant for CodeXplus (https://codexplus.site), a full-stack software house offering:
- Mobile App & Web Application Development (Next.js, React, Node.js, Python, Custom Software)
- Graphic Design & Digital Art
- Social Media Marketing, SEO & GEO (Generative Engine Optimization)
- Arduino Hardware & Embedded Software Prototyping on client demand

Analyze this post and decide if it is a REAL CLIENT looking to hire or place an order with a budget:
Platform: {lead['platform']}
Title: {lead['title']}
Text: {lead['raw_text']}

STRICT RULES:
1. Score 1-2 if the author is a developer, designer, marketer, or agency advertising their own services ("for hire", "portfolio", "I build", "DM me for services") or generic job board spam.
2. Score 7-10 ONLY if a client/founder/business is actively looking to hire someone or place an order for app, web, software, graphic design/art, SEO/GEO, social media marketing, or Arduino hardware/software. Give 9-10 if a budget ($) is mentioned.
3. Write a concise, natural, high-converting outreach reply from CodeXplus (https://codexplus.site) tailored directly to their request.

Respond ONLY in valid JSON:
{{"score": 8, "summary": "1-sentence summary including budget if stated", "reply": "Direct tailored pitch mentioning https://codexplus.site"}}"""

    raw = call_gemini(prompt)
    cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    parsed = json.loads(cleaned)
    score = int(parsed.get("score", 1))
    summary = str(parsed.get("summary", lead["title"]))
    reply = str(parsed.get("reply", ""))
    return score, summary, reply


def regenerate_lead_reply(lead: dict) -> str:
    prompt = f"""Write a fresh, concise, high-converting client outreach message from CodeXplus (https://codexplus.site) for this client lead.
CodeXplus services: Web & Mobile Apps, Custom Software, Graphic Design & Art, Social Media Marketing, SEO/GEO, and Custom Arduino Hardware/Software.

Platform: {lead.get('platform', '')}
Title: {lead.get('title', '')}
Post: {lead.get('raw_text', '')}

Keep it direct, friendly, under 80 words, focused on solving their exact project, and include https://codexplus.site naturally. Return only the reply text."""
    return call_gemini(prompt)


def fetch_arctic_shift_reddit() -> list[dict]:
    subreddits = ["forhire", "freelance_forhire", "DesignJobs", "slavelabour", "hiring", "arduino"]
    items = []
    now_ts = int(time.time())
    after_ts = now_ts - 7200  # Last 2 hours

    with httpx.Client(timeout=15.0) as client:
        for sub in subreddits:
            try:
                resp = client.get(
                    "https://arctic-shift.photon-reddit.com/api/posts/search",
                    params={"subreddit": sub, "after": after_ts, "limit": 15, "sort": "desc"},
                )
                if resp.status_code != 200:
                    continue
                for post in resp.json().get("data", []):
                    title = post.get("title", "")
                    body = post.get("selftext", "")
                    num_comments = post.get("num_comments", 0)
                    if num_comments > 4:
                        continue
                    lower_title = title.lower()
                    if "[for hire]" in lower_title or "for hire" in lower_title:
                        continue
                    if "[hiring]" not in lower_title and "looking to hire" not in lower_title and "need" not in lower_title:
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
    custom_x = get_setting("x_query")
    queries = [
        ("X", custom_x or '(site:x.com OR site:twitter.com) ("looking to hire" OR "need a developer" OR "need a website" OR "need an app" OR "graphic designer" OR "SEO") ("budget" OR "$") -"for hire"'),
        ("Threads", 'site:threads.net ("looking to hire" OR "need a website" OR "need an app" OR "graphic designer") -"for hire"'),
        ("Facebook", 'site:facebook.com ("looking to hire" OR "need a web developer" OR "need an app" OR "digital marketing agency") ("budget" OR "$") -"for hire"'),
        ("Telegram", 'site:t.me ("looking to hire" OR "need developer" OR "website project" OR "app project") ("budget" OR "$") -"for hire"'),
        ("Discord", '(site:discord.com OR site:discord.gg) ("looking to hire" OR "paid project" OR "commission" OR "developer needed") ("$" OR "budget") -"for hire"'),
        ("Twist", 'site:twist.com ("looking to hire" OR "project" OR "developer" OR "designer") ("budget" OR "$")'),
        ("LinkedIn", 'site:linkedin.com/posts ("looking to hire" OR "need an agency" OR "looking for a developer" OR "website redesign") ("budget" OR "proposal") -"open to work"'),
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


def run_sync() -> dict:
    serper_key = get_setting("serper_api_key")
    key_pool = get_gemini_key_pool()

    candidates = fetch_arctic_shift_reddit() + fetch_serper_channels(serper_key)
    conn = get_db()
    cur = conn.cursor()

    saved = 0
    qualified = 0
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
                score, summary, reply = score_and_draft(item)
                time.sleep(0.35)
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
        if score >= 7:
            qualified += 1

    conn.commit()
    conn.close()
    return {
        "fetched": len(candidates),
        "saved": saved,
        "qualified": qualified,
        "notes": "; ".join(notes[:3]),
    }