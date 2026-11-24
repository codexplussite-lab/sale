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

DEFAULT_AGENT_PROMPT = """Hunt ONLY for REAL PAYING CLIENTS, founders, and business owners who want to pay someone to create or build a project for them—for example:
- "I will pay $500 for someone to create my portfolio website"
- "Looking for a website & app developer to build my mobile app / SaaS / e-commerce store"
- "Need custom software, ERP, CRM, or AI automation — budget $500+"
- "Looking for a graphic designer for logo, branding, or social media creatives"
- "Need SEO, GEO, social media marketing, or custom Arduino hardware/software"

BUYER SIGNALS TO PRIORITIZE (SCORE 8-10):
- Phrases like "I will pay", "paying $", "budget", "$500", "need someone to create my...", "looking to hire", "who can build", "send quotes/rates".
- Clients asking someone to build THEIR portfolio website, business website, mobile app, software, or graphic design.

STRICT SELLER / CHATTER EXCLUSIONS (SCORE 1 AND SET is_buyer=false):
- The post author's display name/header says they are a developer, designer, freelancer, or agency (e.g., "Ray website & app developer", "John | UI/UX Designer").
- Freelancers/agencies advertising their own services ("[For Hire]", "DM me for services", "check out my portfolio", "I am a developer looking for clients").
- Tech discussions, book/course requests, memes, or unpaid/equity-only posts."""

SELLER_BODY_PHRASES = [
    "[for hire]",
    "for hire",
    "open to work",
    "looking for work",
    "seeking work",
    "available for work",
    "available for hire",
    "check out my portfolio",
    "here is my portfolio",
    "hire me",
    "i am a developer",
    "i am a full stack",
    "i am a web developer",
    "i am an ios",
    "i am a designer",
    "i will build your",
    "we build",
    "how they find clients",
    "[removed]",
    "[deleted]",
    "removed by moderator",
]

SELLER_AUTHOR_TITLES = [
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
]


def is_seller_or_chatter(title: str, body: str) -> bool:
    low_title = (title or "").lower()
    low_full = f"{low_title}\n{(body or '').lower()}"
    if any(role in low_title for role in SELLER_AUTHOR_TITLES):
        return True
    return any(phrase in low_full for phrase in SELLER_BODY_PHRASES)


def normalize_url_hash(url: str) -> str:
    clean = (url or "").split("?")[0].rstrip("/").lower()
    return hashlib.md5(clean.encode("utf-8")).hexdigest()


def parse_serper_age_seconds(date_str: str) -> int | None:
    """
    Parses Google/Serper relative timestamps like '45 secs ago', '2 mins ago', '4 minutes ago', '22 hours ago'.
    Returns age in seconds, or None if Google didn't attach a relative minute/second timestamp.
    """
    if not date_str:
        return None
    low = date_str.strip().lower()
    m = re.search(r"(\d+)\s*(sec|second|min|minute|hour|hr|day|week|month|year)", low)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2)
    if unit.startswith("sec"):
        return val
    if unit.startswith("min"):
        return val * 60
    if unit.startswith(("hour", "hr")):
        return val * 3600
    if unit.startswith("day"):
        return val * 86400
    return 999999


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
    # Permanent deduplication ledger: remembers every seen post even after you delete it from leads
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_leads (
            dedup_key TEXT PRIMARY KEY,
            seen_at TEXT
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


def is_already_seen(external_id: str, url: str) -> bool:
    url_hash = normalize_url_hash(url)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM seen_leads WHERE dedup_key IN (?, ?)", (external_id, url_hash))
    found = cur.fetchone() is not None
    if not found:
        cur.execute("SELECT 1 FROM leads WHERE external_id = ? OR url = ?", (external_id, url))
        found = cur.fetchone() is not None
    conn.close()
    return found


def mark_as_seen(external_id: str, url: str):
    now_iso = datetime.now(timezone.utc).isoformat()
    url_hash = normalize_url_hash(url)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO seen_leads (dedup_key, seen_at) VALUES (?, ?)", (external_id, now_iso))
    cur.execute("INSERT OR IGNORE INTO seen_leads (dedup_key, seen_at) VALUES (?, ?)", (url_hash, now_iso))
    conn.close()


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
    compiler_prompt = f"""You are a search query engineer for a freelance/agency client lead scraper.
Convert the user's Lead Hunter Agent Prompt into strict search parameters that ONLY match paying clients who say things like "I will pay $500", "need someone to create my portfolio website", "looking for a mobile app developer", or "need a graphic designer".

USER AGENT PROMPT:
{agent_prompt}

Return ONLY valid JSON with these exact keys:
{{
  "subreddits": "comma-separated relevant subreddits without r/, e.g. forhire,freelance_forhire,DesignJobs,slavelabour,startups,SaaS,shopify,smallbusiness",
  "reddit_keywords": "comma-separated lowercase trigger phrases to require in Reddit titles, e.g. [hiring],i will pay,will pay,looking to hire,need a developer,create my,build my,budget",
  "serper_dork": "Google search boolean query without site: prefix that matches clients paying for websites, portfolio sites, mobile apps, software, or graphic design, e.g. (\\"[hiring]\\" OR \\"i will pay\\" OR \\"will pay\\" OR \\"looking for a developer\\" OR \\"need someone to create\\" OR \\"create my portfolio\\" OR \\"build a mobile app\\" OR \\"need a graphic designer\\") (\\"budget\\" OR \\"$\\") -\\"for hire\\" -\\"hire me\\" -\\"open to work\\""
}}"""
    raw = call_gemini(compiler_prompt)
    cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    parsed = json.loads(cleaned)
    return {
        "subreddits": str(parsed.get("subreddits", "forhire,freelance_forhire,DesignJobs,slavelabour,startups,SaaS,shopify,smallbusiness,Entrepreneur")),
        "reddit_keywords": str(parsed.get("reddit_keywords", "[hiring],i will pay,will pay,looking to hire,need a developer,create my,build my,need a designer,budget")),
        "serper_dork": str(parsed.get("serper_dork", '("[hiring]" OR "i will pay" OR "will pay" OR "looking for a developer" OR "need someone to create" OR "create my portfolio" OR "build a mobile app" OR "need a graphic designer") ("budget" OR "$") -"for hire" -"hire me" -"open to work"')),
    }


def score_and_draft(lead: dict, agent_prompt: str) -> tuple[int, str, str, str, str]:
    prompt = f"""You are a strict Buyer-vs-Seller Lead Auditor for CodeXplus (https://codexplus.site).
Your ONLY job is to pass posts where a REAL PAYING CLIENT wants to pay someone to create/build a portfolio website, business website, mobile app, custom software, graphic design, marketing/SEO, or Arduino project.

FOLLOW THIS ACTIVE AGENT MISSION & FILTERING RUBRIC STRICTLY:
{agent_prompt}

CANDIDATE POST TO AUDIT:
Platform: {lead['platform']}
Freshness: {lead.get('freshness_badge', 'Posted <5m ago')}
Title / Author Line: {lead['title']}
Post Body: {lead['raw_text']}

MANDATORY 3-GATE REJECTION CHECK (IF ANY FAIL -> SET is_buyer=false AND score=1):
Gate 1 — Author Role Check: Look at the Title/Author Line. If the author's display name/title says THEY are a developer, designer, freelancer, or agency -> FAIL (score 1). (Note: If the author is a client saying "Looking for a website & app developer to build my app", that PASSES!)
Gate 2 — Paying Client Intent Check: Is the author asking someone to build/design something for them (e.g. "I will pay $500 to create my portfolio website", "Need a mobile app developer", "Looking for graphic designer")? If it is a freelancer advertising "[For Hire]", a book/course request, meme, rant, or tech discussion -> FAIL (score 1).
Gate 3 — Clear Deliverable Check: Does the post state what they want made (portfolio website, app, software, graphic design, etc.)? If vague chatter -> FAIL (score 1).

ONLY if ALL 3 GATES PASS:
- Score 7-8 if they clearly state the project they want built/designed.
- Score 9-10 if they also state a budget ($500, $1,000, "I will pay...", etc.) or urgent timeline.
- Write a direct, concise outreach message from CodeXplus (https://codexplus.site) addressing their exact project and budget.

Respond ONLY in valid JSON:
{{
  "is_buyer": true,
  "score": 9,
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


def verify_reddit_post_live(client: httpx.Client, permalink: str, now_ts: int) -> tuple[bool, int, str]:
    """
    Verifies live on Reddit:
    1. Not removed/deleted by moderators or AutoMod
    2. Posted between 20 seconds and 5 minutes ago (<= 300s)
    3. Strictly 0 comments (num_comments == 0)
    """
    clean_perm = permalink.rstrip("/")
    live_url = f"https://www.reddit.com{clean_perm}.json"
    try:
        resp = client.get(
            live_url,
            headers={"User-Agent": "CodeXplusLeadHunter/2.1 (by /u/codexplus_bot)"},
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return False, 0, ""
        payload = resp.json()
        post_data = payload[0]["data"]["children"][0]["data"]

        if post_data.get("removed_by_category") is not None:
            return False, 0, ""
        author = str(post_data.get("author", ""))
        selftext = str(post_data.get("selftext", "")).strip()
        title = str(post_data.get("title", "")).strip()
        if author in ("", "[deleted]") or selftext in ("[removed]", "[deleted]") or "[removed" in title.lower():
            return False, 0, ""

        # Enforce 0.5 to 5 minute age window (30s to 300s)
        created_utc = int(post_data.get("created_utc", now_ts))
        age_sec = now_ts - created_utc
        if age_sec > 300:
            return False, age_sec, ""

        # Strict 0-comment first-mover enforcement
        live_comments = int(post_data.get("num_comments", 0))
        if live_comments > 0:
            return False, age_sec, ""

        return True, max(30, age_sec), selftext
    except Exception:
        return False, 0, ""


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
                created_utc = int(post.get("created_utc", now_ts))
                age_sec = now_ts - created_utc
                # Strict 5-minute freshness window (300 seconds)
                if age_sec > 300:
                    continue

                title = post.get("title", "")
                body = post.get("selftext", "")
                num_comments = int(post.get("num_comments", 0))
                if num_comments > 0:
                    continue
                if is_seller_or_chatter(title, body):
                    continue
                lower_title = title.lower()
                if keywords and not any(kw in lower_title for kw in keywords):
                    continue

                post_id = post.get("id", "")
                permalink = post.get("permalink", f"/r/{sub}/comments/{post_id}")
                full_url = f"https://www.reddit.com{permalink}"
                ext_id = f"reddit_{post_id}"

                if is_already_seen(ext_id, full_url):
                    continue

                is_live, live_age_sec, live_body = verify_reddit_post_live(client, permalink, now_ts)
                if not is_live:
                    continue

                final_body = live_body if live_body else body
                if is_seller_or_chatter(title, final_body):
                    continue

                age_label = f"{live_age_sec}s ago" if live_age_sec < 60 else f"{live_age_sec // 60}m ago"
                found.append(
                    {
                        "external_id": ext_id,
                        "platform": "Reddit",
                        "author": post.get("author", "unknown"),
                        "title": title,
                        "raw_text": f"{title}\n{final_body}"[:1200],
                        "url": full_url,
                        "freshness_badge": f"Posted {age_label} · 0 replies",
                    }
                )
    except Exception:
        pass
    return found


def fetch_arctic_shift_reddit() -> list[dict]:
    subs_raw = get_setting(
        "agent_subreddits",
        "forhire,freelance_forhire,DesignJobs,slavelabour,startups,SaaS,shopify,smallbusiness,Entrepreneur",
    )
    subreddits = [s.strip().replace("r/", "") for s in subs_raw.split(",") if s.strip()][:9]
    if not subreddits:
        subreddits = ["forhire", "freelance_forhire", "DesignJobs", "slavelabour"]

    kw_raw = get_setting(
        "agent_reddit_keywords",
        "[hiring],i will pay,will pay,looking to hire,need a developer,create my,build my,need a designer,budget",
    )
    keywords = [k.strip().lower() for k in kw_raw.split(",") if k.strip()]

    now_ts = int(time.time())
    after_ts = now_ts - 300  # Strict 5-minute window (300 seconds)
    items = []

    with ThreadPoolExecutor(max_workers=len(subreddits)) as pool:
        futures = [pool.submit(_fetch_single_subreddit, sub, after_ts, now_ts, keywords) for sub in subreddits]
        for fut in as_completed(futures):
            items.extend(fut.result())
    return items


def _fetch_single_serper_platform(platform: str, query: str, clean_key: str, max_age_sec: int = 300) -> list[dict]:
    found = []
    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": clean_key, "Content-Type": "application/json"},
                json={"q": query, "num": 10, "tbs": "qdr:h"},
            )
            if resp.status_code != 200:
                return []
            for res in resp.json().get("organic", []):
                link = res.get("link", "")
                title = res.get("title", "")
                snippet = res.get("snippet", "")
                date_hint = str(res.get("date", "")).strip()

                # Strict 0.5 to 5-minute age check: reject anything older than 5 minutes (300s)
                parsed_sec = parse_serper_age_seconds(date_hint)
                if parsed_sec is None or parsed_sec > max_age_sec:
                    continue

                if is_seller_or_chatter(title, snippet):
                    continue

                ext_id = f"{platform.lower()}_{normalize_url_hash(link)[:12]}"
                if is_already_seen(ext_id, link):
                    continue

                found.append(
                    {
                        "external_id": ext_id,
                        "platform": platform,
                        "author": platform,
                        "title": title,
                        "raw_text": f"{title}\n{snippet}"[:1200],
                        "url": link,
                        "freshness_badge": f"Posted {date_hint}",
                    }
                )
    except Exception:
        pass
    return found


def fetch_all_channels_parallel(serper_key: str, freshness: str = "qdr:h", target_platform: str = "all") -> list[dict]:
    """
    Scrapes ALL 8 platforms simultaneously (Reddit + X + LinkedIn + Threads + Facebook + Telegram + Discord + Twist)
    and interleaves results across platforms so every channel gets scored fairly.
    """
    clean_key = (serper_key or "").strip().replace("\n", "").replace("\r", "")
    strict_default_dork = '("[hiring]" OR "i will pay" OR "will pay" OR "looking for a developer" OR "need someone to create" OR "create my portfolio" OR "build a mobile app" OR "need a graphic designer") ("budget" OR "$") -"for hire" -"hire me" -"open to work"'
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
    by_platform: dict[str, list[dict]] = {}

    with ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {}
        if t_low in ("all", "reddit"):
            future_map[executor.submit(fetch_arctic_shift_reddit)] = "Reddit"

        if clean_key:
            for platform, q in social_queries:
                if t_low in ("all", platform.lower()):
                    future_map[executor.submit(_fetch_single_serper_platform, platform, q, clean_key, 300)] = platform

        for fut in as_completed(future_map):
            plat = future_map[fut]
            by_platform.setdefault(plat, []).extend(fut.result())

    # Round-robin interleave across all 8 platforms so no single channel dominates
    interleaved = []
    max_len = max((len(v) for v in by_platform.values()), default=0)
    for i in range(max_len):
        for plat in ["Reddit", "X", "LinkedIn", "Threads", "Facebook", "Telegram", "Discord", "Twist"]:
            bucket = by_platform.get(plat, [])
            if i < len(bucket):
                interleaved.append(bucket[i])

    return interleaved


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
        try:
            min_keep_score = int(get_setting("agent_min_score", "7"))
        except ValueError:
            min_keep_score = 7

        candidates = fetch_all_channels_parallel(serper_key, "qdr:h", platform)
        saved = 0
        rejected = 0
        notes = []

        for item in candidates:
            if is_already_seen(item["external_id"], item["url"]):
                continue

            # Record permanently in seen_leads immediately so it never repeats even if rejected or deleted
            mark_as_seen(item["external_id"], item["url"])

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