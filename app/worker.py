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
_LAST_SCAN_STATUS = {
    "running": False,
    "fetched": 0,
    "saved": 0,
    "rejected": 0,
    "notes": "Ready",
    "finished_at": "",
}

AGENTROUTER_DEFAULT_BASE = "https://agentrouter.org/v1"

# Official Google Gemini 4 models (plus 3.5 Flash-Lite low-cost fallback)
GEMINI_4_MODELS = [
    "gemini-4-hydrogen",
    "gemini-4-neon",
    "gemini-4-argon",
    "gemini-3.5-flash-lite",
]

DEFAULT_AGENT_PROMPT = """Hunt ONLY for REAL PAYING CLIENTS, founders, and business owners who want to pay someone to create or build a project for them—for example:
- "I will pay $500 for someone to create my portfolio website"
- "Looking for a website & app developer to build my mobile app / SaaS / e-commerce store"
- "Need custom software, ERP, CRM, or AI automation — budget $500+"
- "Looking for a graphic designer for logo, branding, or social media creatives"
- "Need SEO, GEO, social media marketing, or custom Arduino hardware/software"

BUYER SIGNALS TO PRIORITIZE (SCORE 8-10):
- Phrases like "I will pay", "paying $", "budget", "$500", "need someone to create my...", "looking to hire", "who can build", "looking for a developer/designer".
- Clients asking someone to build THEIR portfolio website, business website, mobile app, software, or graphic design (score 7-8 if the project need is real).

STRICT SELLER / CHATTER EXCLUSIONS (SCORE 1 AND SET is_buyer=false):
- The post author's display name/header says they are a developer, designer, freelancer, or agency.
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

BROAD_BUYER_DORK = '("[hiring]" OR "looking to hire" OR "looking for a developer" OR "need a developer" OR "need a website" OR "need an app" OR "create my portfolio" OR "need a graphic designer" OR "i will pay") -"for hire" -"hire me" -"open to work"'


def get_scan_status() -> dict:
    return dict(_LAST_SCAN_STATUS)


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


def clear_seen_cache() -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM seen_leads")
    count = cur.rowcount
    conn.close()
    return count


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


def call_gemini_only(prompt: str, override_keys: list[tuple[str, str]] | None = None) -> str:
    keys = override_keys if override_keys is not None else get_gemini_key_pool()
    if not keys:
        raise RuntimeError("No Gemini API keys configured. Add Gemini Key #1-#4 in Agent Studio.")

    errors = []

    with httpx.Client(timeout=12.0) as client:
        for key_label, api_key in keys:
            for model in GEMINI_4_MODELS:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
                try:
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

                    err_msg = resp.text[:100].replace("\n", " ")
                    errors.append(f"{key_label} ({model}) HTTP {resp.status_code}: {err_msg}")
                    if resp.status_code in (400, 401, 403, 429):
                        break
                except Exception as exc:
                    errors.append(f"{key_label} ({model}): {exc}")
                    continue

    raise RuntimeError("Gemini 4 pool failed -> " + " | ".join(errors[:3]))


def call_ai(prompt: str, cached_ar_models: list[str] | None = None) -> str:
    """Uses ONLY the 4-key Gemini 4 pool (Hydrogen, Neon, Argon, 3.5 Flash-Lite)."""
    return call_gemini_only(prompt)


def get_agentrouter_models(api_key: str, base_url: str = "") -> list[str]:
    return []


def call_agentrouter(prompt: str, api_key: str, base_url: str = "", models: list[str] | None = None) -> str:
    return call_gemini_only(prompt)


def test_agentrouter_key(api_key: str, base_url: str = "") -> dict:
    return {"ok": False, "detail": "Disabled — running exclusively on Gemini 4 key pool."}


def test_single_gemini_key(api_key: str) -> dict:
    if not api_key or not api_key.strip():
        return {"ok": False, "detail": "Empty key"}
    try:
        out = call_gemini_only("Reply with the single word: OK", [("Test Key", api_key.strip())])
        return {"ok": True, "detail": f"Active ({out[:20]})"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:180]}


def generate_queries_from_prompt(agent_prompt: str) -> dict:
    compiler_prompt = f"""You are a search query engineer for a freelance/agency client lead scraper.
Convert the user's Lead Hunter Agent Prompt into high-recall search parameters that match real clients looking for developers, designers, websites, mobile apps, software, or marketing—without over-restricting so results actually come back.

USER AGENT PROMPT:
{agent_prompt}

Return ONLY valid JSON with these exact keys:
{{
  "subreddits": "comma-separated relevant subreddits without r/, e.g. forhire,freelance_forhire,DesignJobs,slavelabour,startups,SaaS,shopify,smallbusiness",
  "reddit_keywords": "comma-separated lowercase trigger phrases to match in Reddit titles/posts, e.g. [hiring],looking to hire,looking for a,need a,want to build,create my,build my,budget",
  "serper_dork": "Google search boolean query without site: prefix that finds clients looking to hire without requiring exact long sentences, e.g. (\\"[hiring]\\" OR \\"looking to hire\\" OR \\"looking for a developer\\" OR \\"need a developer\\" OR \\"need a website\\" OR \\"need an app\\" OR \\"need a graphic designer\\") -\\"for hire\\" -\\"hire me\\" -\\"open to work\\""
}}"""
    raw = call_ai(compiler_prompt)
    cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    parsed = json.loads(cleaned)
    return {
        "subreddits": str(parsed.get("subreddits", "forhire,freelance_forhire,DesignJobs,slavelabour,startups,SaaS,shopify,smallbusiness,Entrepreneur")),
        "reddit_keywords": str(parsed.get("reddit_keywords", "[hiring],looking to hire,looking for a,need a,want to build,create my,build my,i will pay,budget")),
        "serper_dork": str(parsed.get("serper_dork", BROAD_BUYER_DORK)),
    }


def score_and_draft(lead: dict, agent_prompt: str, cached_ar_models: list[str] | None = None) -> tuple[int, str, str, str, str]:
    prompt = f"""You are a strict Buyer-vs-Seller Lead Auditor for CodeXplus (https://codexplus.site).
Your ONLY job is to pass posts where a REAL PAYING CLIENT wants to hire someone to create/build a portfolio website, business website, mobile app, custom software, graphic design, marketing/SEO, or Arduino project.

FOLLOW THIS ACTIVE AGENT MISSION & FILTERING RUBRIC STRICTLY:
{agent_prompt}

CANDIDATE POST TO AUDIT:
Platform: {lead['platform']}
Freshness: {lead.get('freshness_badge', 'Fresh post')}
Title / Author Line: {lead['title']}
Post Body: {lead['raw_text']}

MANDATORY 3-GATE REJECTION CHECK (IF ANY FAIL -> SET is_buyer=false AND score=1):
Gate 1 — Author Role Check: Look at the Title/Author Line. If the author's display name/title says THEY are a developer, designer, freelancer, or agency -> FAIL (score 1). (Note: If the author is a client saying "Looking for a website & app developer to build my app", that PASSES!)
Gate 2 — Paying Client Intent Check: Is the author asking someone to build/design something for them (e.g. "I will pay $500 to create my portfolio website", "Need a mobile app developer", "Looking for graphic designer")? If it is a freelancer advertising "[For Hire]", a book/course request, meme, rant, or tech discussion -> FAIL (score 1).
Gate 3 — Clear Deliverable Check: Does the post state what they want made (portfolio website, app, software, graphic design, etc.)? If vague chatter -> FAIL (score 1).

ONLY if ALL 3 GATES PASS:
- Score 7-8 if they clearly state the project they want built/designed (even if budget is not yet mentioned).
- Score 9-10 if they also state a budget ($500, $1,000, "I will pay...", etc.) or urgent timeline.
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

    raw = call_ai(prompt)
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
    return call_ai(prompt)


def _fetch_single_subreddit(sub: str, after_ts: int, now_ts: int, keywords: list[str], max_comments: int) -> list[dict]:
    found = []
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(
                "https://arctic-shift.photon-reddit.com/api/posts/search",
                params={"subreddit": sub, "after": after_ts, "limit": 20, "sort": "desc"},
            )
            if resp.status_code != 200:
                return []
            for post in resp.json().get("data", []):
                title = post.get("title", "")
                body = post.get("selftext", "")
                author = str(post.get("author", ""))
                if author in ("", "[deleted]") or body.strip() in ("[removed]", "[deleted]"):
                    continue
                num_comments = int(post.get("num_comments", 0))
                if num_comments > max_comments:
                    continue
                if is_seller_or_chatter(title, body):
                    continue
                lower_combo = f"{title.lower()} {body[:300].lower()}"
                if keywords and not any(kw in lower_combo for kw in keywords):
                    continue

                post_id = post.get("id", "")
                permalink = post.get("permalink", f"/r/{sub}/comments/{post_id}")
                full_url = f"https://www.reddit.com{permalink}"
                ext_id = f"reddit_{post_id}"

                if is_already_seen(ext_id, full_url):
                    continue

                created_utc = int(post.get("created_utc", now_ts))
                age_sec = max(30, now_ts - created_utc)
                age_label = f"{age_sec}s ago" if age_sec < 60 else f"{age_sec // 60}m ago"
                found.append(
                    {
                        "external_id": ext_id,
                        "platform": "Reddit",
                        "author": author or "unknown",
                        "title": title,
                        "raw_text": f"{title}\n{body}"[:1200],
                        "url": full_url,
                        "freshness_badge": f"Posted {age_label} · {num_comments} replies",
                    }
                )
    except Exception:
        pass
    return found


def fetch_arctic_shift_reddit(freshness: str = "qdr:h") -> list[dict]:
    subs_raw = get_setting(
        "agent_subreddits",
        "forhire,freelance_forhire,DesignJobs,slavelabour,startups,SaaS,shopify,smallbusiness,Entrepreneur",
    )
    subreddits = [s.strip().replace("r/", "") for s in subs_raw.split(",") if s.strip()][:9]
    if not subreddits:
        subreddits = ["forhire", "freelance_forhire", "DesignJobs", "slavelabour"]

    kw_raw = get_setting(
        "agent_reddit_keywords",
        "[hiring],looking to hire,looking for a,need a,want to build,create my,build my,i will pay,budget",
    )
    keywords = [k.strip().lower() for k in kw_raw.split(",") if k.strip()]

    if freshness == "qdr:h":
        max_age_sec = 3600
        max_comments = 2
    elif freshness == "qdr:d":
        max_age_sec = 86400
        max_comments = 3
    else:
        max_age_sec = 259200
        max_comments = 4

    now_ts = int(time.time())
    after_ts = now_ts - max_age_sec
    items = []

    with ThreadPoolExecutor(max_workers=len(subreddits)) as pool:
        futures = [
            pool.submit(_fetch_single_subreddit, sub, after_ts, now_ts, keywords, max_comments)
            for sub in subreddits
        ]
        for fut in as_completed(futures, timeout=12.0):
            try:
                items.extend(fut.result())
            except Exception:
                pass
    return items


def _fetch_single_serper_platform(platform: str, query: str, clean_key: str, tbs_val: str) -> list[dict]:
    found = []
    max_sec = 3600 if tbs_val == "qdr:h" else (86400 if tbs_val == "qdr:d" else 604800)
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": clean_key, "Content-Type": "application/json"},
                json={"q": query, "num": 8, "tbs": tbs_val},
            )
            if resp.status_code != 200:
                return []
            for res in resp.json().get("organic", []):
                link = res.get("link", "")
                title = res.get("title", "")
                snippet = res.get("snippet", "")
                date_hint = str(res.get("date", "")).strip()

                parsed_sec = parse_serper_age_seconds(date_hint)
                if parsed_sec is not None and parsed_sec > max_sec:
                    continue

                if is_seller_or_chatter(title, snippet):
                    continue

                ext_id = f"{platform.lower()}_{normalize_url_hash(link)[:12]}"
                if is_already_seen(ext_id, link):
                    continue

                badge_text = f"Posted {date_hint}" if date_hint else f"Fresh ({tbs_val})"
                found.append(
                    {
                        "external_id": ext_id,
                        "platform": platform,
                        "author": platform,
                        "title": title,
                        "raw_text": f"{title}\n{snippet}"[:1200],
                        "url": link,
                        "freshness_badge": badge_text,
                    }
                )
    except Exception:
        pass
    return found


def fetch_all_channels_parallel(serper_key: str, freshness: str = "qdr:h", target_platform: str = "all") -> list[dict]:
    clean_key = (serper_key or "").strip().replace("\n", "").replace("\r", "")
    tbs_val = (freshness or "qdr:h").strip()
    saved_dork = get_setting("x_query", "")
    if not saved_dork or '"need someone to create"' in saved_dork:
        base_dork = BROAD_BUYER_DORK
    else:
        base_dork = saved_dork

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
            future_map[executor.submit(fetch_arctic_shift_reddit, tbs_val)] = "Reddit"

        if clean_key:
            for platform, q in social_queries:
                if t_low in ("all", platform.lower()):
                    future_map[executor.submit(_fetch_single_serper_platform, platform, q, clean_key, tbs_val)] = platform

        try:
            for fut in as_completed(future_map, timeout=14.0):
                plat = future_map[fut]
                try:
                    by_platform.setdefault(plat, []).extend(fut.result())
                except Exception:
                    pass
        except Exception:
            pass

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
    _LAST_SCAN_STATUS.update(
        {
            "running": True,
            "fetched": 0,
            "saved": 0,
            "rejected": 0,
            "notes": "Fetching fresh posts across all 8 channels...",
        }
    )
    try:
        serper_key = get_setting("serper_api_key")
        gemini_pool = get_gemini_key_pool()

        if not gemini_pool:
            msg = "No Gemini API keys configured. Add Gemini Key #1-#4 in Agent Studio."
            _LAST_SCAN_STATUS.update({"running": False, "notes": msg, "finished_at": datetime.now(timezone.utc).isoformat()})
            return {"fetched": 0, "saved": 0, "rejected": 0, "notes": msg}

        agent_prompt = (custom_prompt or "").strip() or get_setting("agent_prompt", DEFAULT_AGENT_PROMPT)
        active_freshness = (freshness or "").strip() or get_setting("search_freshness", "qdr:h")
        try:
            min_keep_score = int(get_setting("agent_min_score", "7"))
        except ValueError:
            min_keep_score = 7

        candidates = fetch_all_channels_parallel(serper_key, active_freshness, platform)
        unique_candidates = [
            item for item in candidates
            if not is_already_seen(item["external_id"], item["url"])
        ]

        if not unique_candidates:
            msg = f"Scan complete: 0 brand-new posts found in {active_freshness}. Try 'Past 24 Hours' or Reset Seen Cache."
            _LAST_SCAN_STATUS.update(
                {"running": False, "fetched": 0, "saved": 0, "rejected": 0, "notes": msg, "finished_at": datetime.now(timezone.utc).isoformat()}
            )
            return {"fetched": 0, "saved": 0, "rejected": 0, "notes": msg}

        _LAST_SCAN_STATUS["notes"] = f"Qualifying {len(unique_candidates)} candidates via Gemini 4 pool..."

        saved = 0
        rejected = 0
        notes = []

        def _audit_one(item: dict):
            return item, score_and_draft(item, agent_prompt)

        with ThreadPoolExecutor(max_workers=4) as ai_pool:
            futures = [ai_pool.submit(_audit_one, item) for item in unique_candidates[:20]]
            for fut in as_completed(futures):
                try:
                    item, (score, summary, service_category, client_region, reply) = fut.result()
                except Exception as exc:
                    notes.append(str(exc)[:120])
                    continue

                mark_as_seen(item["external_id"], item["url"])

                if score < min_keep_score:
                    rejected += 1
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

        summary_note = (
            f"Scan complete: {len(unique_candidates)} fresh posts checked, {saved} qualified leads saved, {rejected} filtered out."
            + (f" ({'; '.join(set(notes[:2]))})" if notes else "")
        )
        _LAST_SCAN_STATUS.update(
            {
                "running": False,
                "fetched": len(unique_candidates),
                "saved": saved,
                "rejected": rejected,
                "notes": summary_note,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return {
            "fetched": len(unique_candidates),
            "saved": saved,
            "rejected": rejected,
            "notes": summary_note,
        }
    except Exception as fatal_exc:
        err_note = f"Scan stopped: {fatal_exc}"
        _LAST_SCAN_STATUS.update({"running": False, "notes": err_note, "finished_at": datetime.now(timezone.utc).isoformat()})
        return {"fetched": 0, "saved": 0, "rejected": 0, "notes": err_note}
    finally:
        _SYNC_LOCK.release()