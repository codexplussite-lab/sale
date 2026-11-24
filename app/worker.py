import os
import re
import json
import time
import sqlite3
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import httpx

DB_PATH = os.environ.get("DB_PATH", "/app/data/leads.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

_SYNC_LOCK = threading.Lock()

DEFAULT_AGENT_PROMPT = """Hunt ONLY for REAL PAYING CLIENTS, founders, and business owners who are explicitly hiring or requesting quotes to build a project in one of CodeXplus's services:
1. Web or Mobile App Development (React, Next.js, iOS, Android, SaaS, Shopify/WordPress)
2. Custom Software, ERP, CRM, AI Automation, or API Integration
3. Graphic Design, UI/UX, or Branding
4. SEO, GEO, or Social Media Marketing
5. Custom Arduino / Embedded Hardware Prototyping

STRICT REJECTION RULES (MUST SCORE 1 AND SET is_buyer=false):
- Author's display name or bio says they are a developer, designer, freelancer, agency, or engineer (e.g. "website & app developer").
- Rhetorical questions, tech discussions, rants, memes, or book/course requests (e.g. "Can someone write a book...", "How do I find clients...").
- Freelancers/agencies advertising their own services ("for hire", "DM me", "message me", "portfolio", "available for work").
- Any post where the author is NOT clearly looking to hire/pay someone to build a project."""

SELLER_SPAM_PHRASES = [
    "[for hire]",
    "for hire",
    "message me",
    "dm me",
    "open to work",
    "looking for work",
    "seeking work",
    "available for work",
    "available for hire",
    "my portfolio",
    "check out my portfolio",
    "hire me",
    "website & app developer",
    "web & app developer",
    "full stack developer",
    "fullstack developer",
    "software developer",
    "mobile app developer",
    "ios developer",
    "android developer",
    "freelance developer",
    "graphic designer",
    "ui/ux designer",
    "i am a developer",
    "i am a designer",
    "i will build your",
    "we build",
    "how they find clients",
    "find clients",
]


def is_seller_or_chatter(text: str) -> bool:
    low = (text or "").lower()
    return any(phrase in low for phrase in SELLER_SPAM_PHRASES)


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA busy_timeout=30000;")
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
            pain_point_summary TEXT DEFAULT '',
            service_category TEXT DEFAULT 'Web/App Dev',
            client_region TEXT DEFAULT 'Global',
            suggested_reply TEXT,
            status TEXT DEFAULT 'new',
            notes TEXT DEFAULT '',
            created_at TEXT
        )
        """
    )
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(leads)").fetchall()}
    if "pain_point_summary" not in existing_cols:
        cur.execute("ALTER TABLE leads ADD COLUMN pain_point_summary TEXT DEFAULT ''")
    if "service_category" not in existing_cols:
        cur.execute("ALTER TABLE leads ADD COLUMN service_category TEXT DEFAULT 'Web/App Dev'")
    if "client_region" not in existing_cols:
        cur.execute("ALTER TABLE leads ADD COLUMN client_region TEXT DEFAULT 'Global'")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
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
    conn.close()


def get_gemini_key_pool() -> list[tuple[str, str]]:
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
    compiler_prompt = f"""You are a search query engineer for a B2B/freelance client lead scraper.
Convert the user's Lead Hunter Agent Prompt into strict search parameters that ONLY match paying clients hiring someone to build software, apps, websites, design, marketing, or hardware.

USER AGENT PROMPT:
{agent_prompt}

Return ONLY valid JSON with these exact keys:
{{
  "subreddits": "comma-separated relevant subreddits without r/, e.g. forhire,freelance_forhire,DesignJobs,startups,SaaS,shopify,smallbusiness",
  "reddit_keywords": "comma-separated lowercase trigger phrases to require in Reddit titles, e.g. [hiring],looking to hire,need a developer,want to build,budget",
  "serper_dork": "Google search boolean query without site: prefix that matches clients hiring for a project build and excludes developers/chatter, e.g. (\\"[hiring]\\" OR \\"looking to hire a developer\\" OR \\"looking to hire an agency\\" OR \\"need someone to build\\" OR \\"need a web developer\\" OR \\"need an app developer\\") (\\"budget\\" OR \\"$\\") -\\"for hire\\" -\\"message me\\" -\\"dm me\\" -\\"portfolio\\""
}}"""
    raw = call_gemini(compiler_prompt)
    cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    parsed = json.loads(cleaned)
    return {
        "subreddits": str(parsed.get("subreddits", "forhire,freelance_forhire,DesignJobs,startups,SaaS,shopify,ecommerce,smallbusiness,Entrepreneur")),
        "reddit_keywords": str(parsed.get("reddit_keywords", "[hiring],looking to hire,need a developer,want to build,need someone to build,need an agency,store redesign")),
        "serper_dork": str(parsed.get("serper_dork", '("[hiring]" OR "looking to hire a developer" OR "looking to hire an agency" OR "need someone to build" OR "need a web developer" OR "need an app developer") ("budget" OR "$") -"for hire" -"message me" -"dm me" -"portfolio"')),
    }


def score_and_draft(lead: dict, agent_prompt: str) -> tuple[int, str, str, str, str]:
    prompt = f"""You are a strict Buyer-vs-Seller Lead Auditor for CodeXplus (https://codexplus.site).
Your ONLY job is to pass posts where a REAL PAYING CLIENT is actively hiring or asking for quotes to build a software, web, app, design, marketing, or hardware project.

FOLLOW THIS ACTIVE AGENT MISSION & FILTERING RUBRIC STRICTLY:
{agent_prompt}

CANDIDATE POST TO AUDIT:
Platform: {lead['platform']}
Competition Status: {lead.get('freshness_badge', 'Fresh post')}
Title / Author Line: {lead['title']}
Post Body: {lead['raw_text']}

MANDATORY 3-GATE REJECTION CHECK (IF ANY FAIL -> SET is_buyer=false AND score=1):
Gate 1 — Author Check: Does the author's name, bio, or text indicate they are a developer, designer, freelancer, or agency (e.g., "Ray website & app developer", "iOS developer message me", "looking for clients")? If YES -> FAIL (score 1).
Gate 2 — Commercial Hiring Intent Check: Is the author asking to HIRE/PAY someone to build a specific project (app, website, software, design, SEO, Arduino)? If it is a rhetorical question ("Can someone write a book..."), tech advice, meme, rant, or job-seeker post -> FAIL (score 1).
Gate 3 — Scope Clarity Check: Does the post clearly state WHAT needs to be built or solved? If vague chatter -> FAIL (score 1).

ONLY if ALL 3 GATES PASS:
- Score 7-8 if they clearly state the project they are hiring for.
- Score 9-10 if they also state a budget ($500, $1,000, etc.) or urgent timeline.
- Write a direct, concise outreach message from CodeXplus (https://codexplus.site) addressing their exact project and budget.

Respond ONLY in valid JSON:
{{
  "is_buyer": true,
  "score": 8,
  "service_category": "Web/App Dev | Custom Software/AI | Graphic Design | Marketing/SEO | Hardware/IoT",
  "client_region": "USA | UK/Europe | Gulf/UAE | Pakistan | Global",
  "summary": "Client wants to build [specific project] — Budget: [stated budget or Not specified]",
  "reply": "Direct tailored pitch mentioning https://codexplus.site"
}}"""

    raw = call_gemini(prompt)
    cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    parsed = json.loads(cleaned)
    is_buyer = bool(parsed.get("is_buyer", False))
    score = int(parsed.get("score", 1))
    if not is_buyer:
        score = 1
    service_category = str(parsed.get("service_category", "Web/App Dev"))
    client_region = str(parsed.get("client_region", "Global"))
    badge = lead.get("freshness_badge", "")
    summary = str(parsed.get("summary", lead["title"]))
    if badge:
        summary = f"[{badge}] {summary}"
    reply = str(parsed.get("reply", ""))
    return score, summary, service_category, client_region, reply


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


def _fetch_single_subreddit(sub: str, after_ts: int, now_ts: int, keywords: list[str]) -> list[dict]:
    found = []
    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.get(
                "https://arctic-shift.photon-reddit.com/api/posts/search",
                params={"subreddit": sub, "after": after_ts, "limit": 20, "sort": "desc"},
            )
            if resp.status_code != 200:
                return []
            for post in resp.json().get("data", []):
                title = post.get("title", "")
                body = post.get("selftext", "")
                full_text = f"{title}\n{body}"
                num_comments = int(post.get("num_comments", 0))
                if num_comments > 2:
                    continue
                if is_seller_or_chatter(full_text):
                    continue
                lower_title = title.lower()
                if keywords and not any(kw in lower_title for kw in keywords):
                    continue
                created_utc = int(post.get("created_utc", now_ts))
                mins_ago = max(1, (now_ts - created_utc) // 60)
                post_id = post.get("id", "")
                permalink = post.get("permalink", f"/r/{sub}/comments/{post_id}")
                found.append(
                    {
                        "external_id": f"reddit_{post_id}",
                        "platform": "Reddit",
                        "author": post.get("author", "unknown"),
                        "title": title,
                        "raw_text": full_text[:1200],
                        "url": f"https://www.reddit.com{permalink}",
                        "freshness_badge": f"Posted {mins_ago}m ago · {num_comments} replies",
                    }
                )
    except Exception:
        pass
    return found


def fetch_arctic_shift_reddit() -> list[dict]:
    subs_raw = get_setting(
        "agent_subreddits",
        "forhire,freelance_forhire,DesignJobs,startups,SaaS,shopify,ecommerce,smallbusiness,Entrepreneur",
    )
    subreddits = [s.strip().replace("r/", "") for s in subs_raw.split(",") if s.strip()][:9]
    if not subreddits:
        subreddits = ["forhire", "freelance_forhire", "DesignJobs", "hiring"]

    kw_raw = get_setting(
        "agent_reddit_keywords",
        "[hiring],looking to hire,need a developer,want to build,need someone to build,need an agency,store redesign",
    )
    keywords = [k.strip().lower() for k in kw_raw.split(",") if k.strip()]

    now_ts = int(time.time())
    after_ts = now_ts - 2700  # Strict 45-minute window
    items = []

    with ThreadPoolExecutor(max_workers=len(subreddits)) as pool:
        futures = [pool.submit(_fetch_single_subreddit, sub, after_ts, now_ts, keywords) for sub in subreddits]
        for fut in as_completed(futures):
            items.extend(fut.result())
    return items


def _fetch_single_serper_platform(platform: str, query: str, clean_key: str, tbs_val: str) -> list[dict]:
    found = []
    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": clean_key, "Content-Type": "application/json"},
                json={"q": query, "num": 6, "tbs": tbs_val},
            )
            if resp.status_code != 200:
                return []
            for res in resp.json().get("organic", []):
                link = res.get("link", "")
                title = res.get("title", "")
                snippet = res.get("snippet", "")
                full_text = f"{title}\n{snippet}"
                if is_seller_or_chatter(full_text):
                    continue
                date_hint = res.get("date", "Past 1h")
                ext_id = f"{platform.lower()}_{hashlib.md5(link.encode()).hexdigest()[:12]}"
                found.append(
                    {
                        "external_id": ext_id,
                        "platform": platform,
                        "author": platform,
                        "title": title,
                        "raw_text": full_text[:1200],
                        "url": link,
                        "freshness_badge": f"Fresh ({date_hint})",
                    }
                )
    except Exception:
        pass
    return found


def fetch_all_channels_parallel(serper_key: str, freshness: str = "qdr:h", target_platform: str = "all") -> list[dict]:
    clean_key = (serper_key or "").strip().replace("\n", "").replace("\r", "")
    tbs_val = (freshness or "qdr:h").strip()
    strict_default_dork = '("[hiring]" OR "looking to hire a developer" OR "looking to hire an agency" OR "need someone to build" OR "need a web developer" OR "need an app developer") ("budget" OR "$") -"for hire" -"message me" -"dm me" -"portfolio"'
    base_dork = get_setting("x_query", strict_default_dork)

    social_queries = [
        ("X", f"(site:x.com OR site:twitter.com) {base_dork}"),
        ("LinkedIn", f'site:linkedin.com/posts {base_dork} -"open to work"'),
        ("Threads", f"site:threads.net {base_dork}"),
        ("Facebook", f"site:facebook.com {base_dork}"),
        ("Telegram", f"site:t.me {base_dork}"),
        ("Discord", f"(site:discord.com OR site:discord.gg) {base_dork}"),
        ("Twist", f"site:twist.com {base_dork}"),
    ]

    t_low = (target_platform or "all").strip().lower()
    items = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        if t_low in ("all", "reddit"):
            futures.append(executor.submit(fetch_arctic_shift_reddit))

        if clean_key:
            for platform, q in social_queries:
                if t_low in ("all", platform.lower()):
                    futures.append(executor.submit(_fetch_single_serper_platform, platform, q, clean_key, tbs_val))

        for fut in as_completed(futures):
            items.extend(fut.result())

    return items


def run_sync(custom_prompt: str | None = None, freshness: str | None = None, platform: str = "all") -> dict:
    if not _SYNC_LOCK.acquire(blocking=False):
        return {
            "fetched": 0,
            "saved": 0,
            "rejected": 0,
            "notes": "A background scan is already running. Wait a few seconds and refresh.",
        }
    try:
        serper_key = get_setting("serper_api_key")
        key_pool = get_gemini_key_pool()
        if not key_pool:
            return {"fetched": 0, "saved": 0, "rejected": 0, "notes": "No Gemini API keys configured."}

        agent_prompt = (custom_prompt or "").strip() or get_setting("agent_prompt", DEFAULT_AGENT_PROMPT)
        active_freshness = (freshness or "").strip() or get_setting("search_freshness", "qdr:h")
        try:
            min_keep_score = int(get_setting("agent_min_score", "7"))
        except ValueError:
            min_keep_score = 7

        candidates = fetch_all_channels_parallel(serper_key, active_freshness, platform)
        saved = 0
        rejected = 0
        notes = []

        for item in candidates:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM leads WHERE external_id = ?", (item["external_id"],))
            exists = cur.fetchone() is not None
            conn.close()
            if exists:
                continue

            try:
                score, summary, service_category, client_region, reply = score_and_draft(item, agent_prompt)
                time.sleep(0.25)
                if score < min_keep_score:
                    rejected += 1
                    continue
            except Exception as exc:
                notes.append(f"Gemini ({item['platform']}): {exc}")
                continue

            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO leads (
                    external_id, platform, author, title, raw_text, url,
                    score, summary, pain_point_summary, service_category, client_region,
                    suggested_reply, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
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
                    summary,
                    service_category,
                    client_region,
                    reply,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            if cur.rowcount > 0:
                saved += 1
            conn.close()

        return {
            "fetched": len(candidates),
            "saved": saved,
            "rejected": rejected,
            "notes": "; ".join(notes[:3]),
        }
    finally:
        _SYNC_LOCK.release()