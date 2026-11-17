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
    env_val = os.environ.get(key.upper(), "")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    if row and row["value"]:
        return row["value"]
    return env_val or default


def call_gemini(prompt: str, api_key: str) -> str:
    models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    last_err = ""
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        for attempt in range(3):
            try:
                with httpx.Client(timeout=25.0) as client:
                    resp = client.post(
                        url,
                        json={"contents": [{"parts": [{"text": prompt}]}]},
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
                last_err = f"{model} HTTP {resp.status_code}: {resp.text[:120]}"
                if resp.status_code in (429, 503):
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            except Exception as exc:
                last_err = str(exc)
                time.sleep(1)
    raise RuntimeError(f"Gemini failed: {last_err}")


def score_and_draft(lead: dict, api_key: str) -> tuple[int, str, str]:
    prompt = f"""You are a lead qualification assistant for CodeXplus (codexplus.site), a full-stack software house offering:
- Mobile App & Web Application Development (Next.js, React, Node.js, Custom Software)
- Graphic Design & Digital Art
- Social Media Marketing, SEO & GEO (Generative Engine Optimization)
- Arduino Hardware & Embedded Software Prototyping on client demand

Analyze this post and decide if it is a REAL CLIENT looking to hire or place an order with a budget:
Platform: {lead['platform']}
Title: {lead['title']}
Text: {lead['raw_text']}

STRICT RULES:
1. Score 1-2 if the author is a developer, designer, marketer, or agency advertising their own services ("for hire", "portfolio", "I build", "DM me for services") or a generic full-time job board spam.
2. Score 7-10 ONLY if a client/founder/business is actively looking to hire someone or place an order for app, web, software, graphic design/art, SEO/GEO, social media marketing, or Arduino hardware/software. Give 9-10 if a budget ($) is mentioned.
3. Write a concise, natural, non-spammy outreach reply from CodeXplus (codexplus.site) tailored directly to their request.

Respond ONLY in valid JSON:
{{"score": 8, "summary": "1-sentence summary including budget if stated", "reply": "Direct tailored pitch mentioning codexplus.site"}}"""

    raw = call_gemini(prompt, api_key)
    cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    parsed = json.loads(cleaned)
    score = int(parsed.get("score", 1))
    summary = str(parsed.get("summary", lead["title"]))
    reply = str(parsed.get("reply", ""))
    return score, summary, reply


def regenerate_lead_reply(lead: dict) -> str:
    api_key = get_setting("gemini_api_key")
    if not api_key:
        raise RuntimeError("Gemini API key is not configured in Settings or .env.")
    prompt = f"""Write a fresh, concise, high-converting client outreach message from CodeXplus (codexplus.site) for this client lead.
CodeXplus services: Web & Mobile Apps, Custom Software, Graphic Design & Art, Social Media Marketing, SEO/GEO, and Custom Arduino Hardware/Software.

Platform: {lead.get('platform', '')}
Title: {lead.get('title', '')}
Post: {lead.get('raw_text', '')}

Keep it direct, friendly, under 80 words, focused on solving their exact project, and include https://codexplus.site naturally. Return only the reply text."""
    return call_gemini(prompt, api_key)


def fetch_arctic_shift_reddit() -> list[dict]:
    subreddits = ["forhire", "freelance_forhire", "DesignJobs", "slavelabour", "hiring", "arduino"]
    items = []
    now_ts = int(time.time())
    after_ts = now_ts - 3600

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
                    if num_comments > 3:
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
    queries = [
        ("X", '(site:x.com OR site:twitter.com) ("looking to hire" OR "need a developer" OR "need a website" OR "need an app" OR "graphic designer" OR "SEO") ("budget" OR "$") -"for hire"'),
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
    gemini_key = get_setting("gemini_api_key")

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

        if gemini_key:
            try:
                score, summary, reply = score_and_draft(item, gemini_key)
                time.sleep(0.4)
            except Exception as exc:
                notes.append(f"Gemini ({item['external_id']}): {exc}")

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