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

DEFAULT_AGENT_PROMPT = """You are the Senior Lead Qualification Agent for CodeXplus (https://codexplus.site), a full-service software house and digital agency.

OUR 6 CORE SERVICE VERTICALS:
1. Web/App Dev (React.js, Next.js, Django, Node.js/Fastify, Mobile & Desktop apps, Corporate/Sanity CMS/WordPress sites)
2. E-Commerce (Shopify, custom online stores, B2B/B2C headless commerce)
3. ERP/Portal (Accounting & ERP software, school/madarsa portals, medical portals, attendance/payroll, remote employee monitoring)
4. UI/UX & Branding (Figma UI/UX, brand identity, logos, social media graphics)
5. Video/Reels (Commercial promos, motion graphics, Instagram/TikTok reels editing)
6. SMM/SEO (Instagram, Facebook, LinkedIn, X management, content calendars, on-page & technical SEO)

HIGH-INTENT SIGNALS (Score 7-10 — Qualify):
- Founder, business owner, decision-maker, or client looking to hire an agency, developer, editor, or marketer.
- Mentions explicit business pain points (outdated site, manual processes needing custom ERP/portal, low conversion, launching store, need reels/videos).
- Mentions budget ($), urgent timeline, or hiring immediately (Score 8-10).

STRICT REJECTION RULES (Score 1-3 — Auto-Discard):
- Reject any post from freelancers, developers, editors, or agencies promoting their OWN services ("for hire", "portfolio", "I will build", "DM me for", "open for commissions").
- Reject full-time onsite W-2/salary job posts unless open to remote agencies/contractors.
- Reject unpaid internships, equity-only offers, students asking for homework/tutorial help.
- Reject posts older than 30 days or posts with zero buying intent (memes, general discussions).
- Reject ultra-low-budget requests (e.g. $20 website or free work)."""


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
            service_category TEXT DEFAULT 'Web/App Dev',
            client_region TEXT DEFAULT 'Global',
            pain_point_summary TEXT DEFAULT '',
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

    # Safe column migrations for existing databases
    cur.execute("PRAGMA table_info(leads)")
    existing_cols = [col[1] for col in cur.fetchall()]
    if "service_category" not in existing_cols:
        cur.execute("ALTER TABLE leads ADD COLUMN service_category TEXT DEFAULT 'Web/App Dev'")
    if "client_region" not in existing_cols:
        cur.execute("ALTER TABLE leads ADD COLUMN client_region TEXT DEFAULT 'Global'")
    if "pain_point_summary" not in existing_cols:
        cur.execute("ALTER TABLE leads ADD COLUMN pain_point_summary TEXT DEFAULT ''")
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


def call_gemini(
    prompt: str,
    override_keys: list[tuple[str, str]] | None = None,
    response_json: bool = False,
) -> str:
    """
    Calls Gemini API with automatic model failover and 4-key pool rotation.
    Supports responseMimeType='application/json' for strict structured JSON.
    """
    keys = override_keys if override_keys is not None else get_gemini_key_pool()
    if not keys:
        raise RuntimeError("No Gemini API keys configured. Add Key #1-#4 in Admin Panel or .env.")

    # High-quota and stable models ordered for optimal performance
    models = [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-2.5-pro",
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
    ]
    errors = []

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
    }
    if response_json:
        body["generationConfig"] = {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        }

    for key_label, api_key in keys:
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            try:
                with httpx.Client(timeout=25.0) as client:
                    resp = client.post(url, json=body)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        return candidates[0]["content"]["parts"][0]["text"].strip()
                    errors.append(f"{key_label} ({model}): Empty response")
                    continue

                # If 400 because responseMimeType is unsupported on older endpoint, fallback to plain prompt
                if resp.status_code == 400 and response_json:
                    try:
                        fallback_body = {"contents": [{"parts": [{"text": prompt}]}]}
                        with httpx.Client(timeout=25.0) as client:
                            resp2 = client.post(url, json=fallback_body)
                        if resp2.status_code == 200:
                            data2 = resp2.json()
                            candidates2 = data2.get("candidates", [])
                            if candidates2:
                                return candidates2[0]["content"]["parts"][0]["text"].strip()
                    except Exception:
                        pass

                err_msg = resp.text[:140].replace("\n", " ")
                errors.append(f"{key_label} ({model}) HTTP {resp.status_code}: {err_msg}")
                if resp.status_code in (401, 403):
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
    """Uses Gemini to compile a plain-English agent prompt into Reddit & Serper search dorks targeting buyer intent."""
    compiler_prompt = f"""You are an elite search query engineer specializing in agency B2B client acquisition for CodeXplus.
CodeXplus offers: Web/App Dev, E-Commerce (Shopify), ERP/Portals, UI/UX & Branding, Video/Reels, SMM/SEO.

CONVERT THIS LEAD HUNTER PROMPT INTO TARGETED SEARCH PARAMETERS:
{agent_prompt}

REQUIREMENTS:
1. Subreddits: list high-converting client subreddits (e.g. forhire,freelance_forhire,DesignJobs,startups,SaaS,shopify,ecommerce,smallbusiness,Entrepreneur).
2. Reddit Keywords: trigger phrases indicating hiring intent (e.g. [hiring],looking to hire,need a developer,need an agency,recommend a dev,store redesign,need custom erp,hiring a video editor,looking for an seo).
3. Serper Dork: Google boolean query targeting buyers with budget/urgency while strictly excluding freelancers/agencies promoting themselves. Must include -intitle:"for hire" -"for hire" -"open to work" -"I will build".

Return ONLY valid JSON:
{{
  "subreddits": "comma-separated subreddits without r/",
  "reddit_keywords": "comma-separated lowercase trigger phrases",
  "serper_dork": "Google search boolean query without site: prefix"
}}"""
    try:
        raw = call_gemini(compiler_prompt, response_json=True)
        cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
        parsed = json.loads(cleaned)
        return {
            "subreddits": str(parsed.get("subreddits", "forhire,freelance_forhire,DesignJobs,startups,SaaS,shopify,ecommerce,smallbusiness")),
            "reddit_keywords": str(parsed.get("reddit_keywords", "[hiring],looking to hire,need a developer,need an agency,recommend a dev,store redesign,need custom erp,hiring a video editor")),
            "serper_dork": str(parsed.get("serper_dork", '("looking for a developer" OR "need an agency" OR "looking to hire" OR "recommend a dev" OR "hiring a video editor" OR "need custom ERP" OR "looking for an SEO") ("budget" OR "$" OR "proposal") -intitle:"for hire" -"for hire" -"open to work" -"I will build"')),
        }
    except Exception:
        return {
            "subreddits": "forhire,freelance_forhire,DesignJobs,startups,SaaS,shopify,ecommerce,smallbusiness",
            "reddit_keywords": "[hiring],looking to hire,need a developer,need an agency,recommend a dev,store redesign,need custom erp,hiring a video editor",
            "serper_dork": '("looking for a developer" OR "need an agency" OR "looking to hire" OR "recommend a dev" OR "hiring a video editor" OR "need custom ERP" OR "looking for an SEO") ("budget" OR "$" OR "proposal") -intitle:"for hire" -"for hire" -"open to work" -"I will build"',
        }


def score_and_draft(lead: dict, agent_prompt: str) -> dict:
    """
    Evaluates a candidate lead and returns structured qualification data:
    score, service_category, client_region, pain_point_summary, suggested_codexplus_pitch.
    """
    prompt = f"""You are the Senior Lead Qualification Agent for CodeXplus (https://codexplus.site).
CodeXplus is a software house & digital agency offering:
1. Web/App Dev (React.js, Next.js, Django, Node.js/Fastify, Mobile & Desktop apps, Corporate/CMS)
2. E-Commerce (Shopify, custom online stores, B2B/B2C headless commerce)
3. ERP/Portal (Accounting & ERP, school/madarsa portals, medical portals, attendance/payroll, monitoring)
4. UI/UX & Branding (Figma UI/UX, brand identity, logos, social media graphics)
5. Video/Reels (Commercial promos, motion graphics, Instagram/TikTok reels editing)
6. SMM/SEO (Social media management, content calendars, on-page & technical SEO)

FOLLOW THIS ACTIVE AGENT RUBRIC:
{agent_prompt}

CANDIDATE LEAD TO EVALUATE:
Platform: {lead.get('platform', '')}
Title: {lead.get('title', '')}
Text: {lead.get('raw_text', '')}

SCORING RULES:
- Score 1 to 3 (Auto-Discard): Freelancers/agencies promoting themselves ("for hire", "portfolio", "I will build"), full-time onsite W-2 jobs, unpaid internships, homework requests, or zero buying intent.
- Score 7 to 10 (Qualify): Real founder/business owner looking to hire an agency/developer/designer/editor/marketer for one of CodeXplus's 6 services. Score 8-10 if budget ($) or urgent kickoff is mentioned.

OUTPUT FORMAT (Strict JSON):
Return ONLY a valid JSON object matching this exact schema:
{{
  "score": <integer 1 to 10>,
  "service_category": "<Web/App Dev | E-Commerce | ERP/Portal | UI/UX & Branding | Video/Reels | SMM/SEO>",
  "client_region": "<USA | UK/Europe | UAE/Gulf | Pakistan | Global>",
  "pain_point_summary": "<1-sentence summary of what the client actually needs>",
  "suggested_codexplus_pitch": "<2-3 sentence tailored, human-sounding outreach hook positioning CodeXplus for their exact requirement without sounding spammy>"
}}"""

    raw = call_gemini(prompt, response_json=True)
    cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    parsed = json.loads(cleaned)

    score = int(parsed.get("score", 1))
    service_cat = str(parsed.get("service_category", "Web/App Dev"))
    client_region = str(parsed.get("client_region", "Global"))
    pain_point = str(parsed.get("pain_point_summary", lead.get("title", "")))
    pitch = str(parsed.get("suggested_codexplus_pitch", ""))

    return {
        "score": score,
        "service_category": service_cat,
        "client_region": client_region,
        "pain_point_summary": pain_point,
        "suggested_codexplus_pitch": pitch,
    }


def regenerate_lead_reply(lead: dict) -> str:
    agent_prompt = get_setting("agent_prompt", DEFAULT_AGENT_PROMPT)
    prompt = f"""You are the Senior Outreach Specialist for CodeXplus (https://codexplus.site).
Write a high-converting, human, 2-3 sentence outreach hook tailored specifically to this client's pain points.
Position CodeXplus directly as the ideal partner for their exact need without sounding robotic or spammy. Naturally incorporate https://codexplus.site.

CONTEXT:
{agent_prompt}

LEAD DETAILS:
Platform: {lead.get('platform', '')}
Service Category: {lead.get('service_category', 'Web/App Dev')}
Client Need: {lead.get('pain_point_summary', lead.get('summary', ''))}
Raw Post: {lead.get('raw_text', '')}

Return ONLY the outreach message text."""
    return call_gemini(prompt)


def fetch_arctic_shift_reddit(freshness: str = "qdr:d") -> list[dict]:
    subs_raw = get_setting(
        "agent_subreddits",
        "forhire,freelance_forhire,DesignJobs,startups,SaaS,shopify,ecommerce,smallbusiness,hiring",
    )
    subreddits = [s.strip().replace("r/", "") for s in subs_raw.split(",") if s.strip()]
    if not subreddits:
        subreddits = ["forhire", "freelance_forhire", "DesignJobs", "startups", "shopify"]

    kw_raw = get_setting(
        "agent_reddit_keywords",
        "[hiring],looking to hire,need a developer,need an agency,recommend a dev,store redesign,need custom erp,hiring a video editor",
    )
    keywords = [k.strip().lower() for k in kw_raw.split(",") if k.strip()]

    now_ts = int(time.time())
    if freshness == "qdr:m":
        after_ts = now_ts - (30 * 86400)
    elif freshness == "qdr:w":
        after_ts = now_ts - (7 * 86400)
    else:  # Default qdr:d (Last 24 Hours)
        after_ts = now_ts - 86400

    items = []
    with httpx.Client(timeout=15.0) as client:
        for sub in subreddits[:10]:
            try:
                resp = client.get(
                    "https://arctic-shift.photon-reddit.com/api/posts/search",
                    params={"subreddit": sub, "after": after_ts, "limit": 25, "sort": "desc"},
                )
                if resp.status_code != 200:
                    continue
                for post in resp.json().get("data", []):
                    title = post.get("title", "")
                    body = post.get("selftext", "")
                    num_comments = post.get("num_comments", 0)
                    if num_comments > 12:
                        continue
                    lower_title = title.lower()
                    if "[for hire]" in lower_title or "for hire" in lower_title or "[forhire]" in lower_title:
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
                            "raw_text": f"{title}\n{body}"[:1400],
                            "url": f"https://www.reddit.com{permalink}",
                        }
                    )
            except Exception:
                continue
    return items


def fetch_serper_channels(serper_key: str, freshness: str = "qdr:d") -> list[dict]:
    if not serper_key:
        return []

    base_dork = get_setting(
        "x_query",
        '("looking for a developer" OR "need an agency" OR "looking to hire" OR "recommend a dev" OR "hiring a video editor" OR "need custom ERP" OR "looking for an SEO") ("budget" OR "$" OR "proposal") -intitle:"for hire" -"for hire" -"open to work" -"I will build"',
    )

    valid_tbs = freshness if freshness in ("qdr:d", "qdr:w", "qdr:m") else "qdr:d"

    queries = [
        ("X", f"(site:x.com OR site:twitter.com) {base_dork} -filter:retweets"),
        ("LinkedIn", f'site:linkedin.com/posts {base_dork}'),
        ("Reddit", f'site:reddit.com {base_dork}'),
        ("Threads", f"site:threads.net {base_dork}"),
        ("Facebook", f"site:facebook.com {base_dork}"),
        ("Telegram", f"site:t.me {base_dork}"),
        ("Discord", f"(site:discord.com OR site:discord.gg) {base_dork}"),
        ("Twist", f"site:twist.com {base_dork}"),
    ]

    items = []
    with httpx.Client(timeout=15.0) as client:
        for platform, q in queries:
            try:
                resp = client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                    json={"q": q, "num": 10, "tbs": valid_tbs},
                )
                if resp.status_code != 200:
                    continue
                for res in resp.json().get("organic", []):
                    link = res.get("link", "")
                    title = res.get("title", "")
                    snippet = res.get("snippet", "")
                    if not link:
                        continue
                    ext_id = f"{platform.lower()}_{hashlib.md5(link.encode()).hexdigest()[:12]}"
                    items.append(
                        {
                            "external_id": ext_id,
                            "platform": platform,
                            "author": platform,
                            "title": title,
                            "raw_text": f"{title}\n{snippet}"[:1400],
                            "url": link,
                        }
                    )
            except Exception:
                continue
    return items


def run_sync(custom_prompt: str | None = None, freshness: str | None = None) -> dict:
    serper_key = get_setting("serper_api_key")
    key_pool = get_gemini_key_pool()
    agent_prompt = (custom_prompt or "").strip() or get_setting("agent_prompt", DEFAULT_AGENT_PROMPT)
    freshness_mode = (freshness or "").strip() or get_setting("search_freshness", "qdr:d")

    try:
        min_keep_score = int(get_setting("agent_min_score", "7"))
    except ValueError:
        min_keep_score = 7

    # 1. Fetch raw candidates from multi-channel sources using date freshness
    candidates = fetch_arctic_shift_reddit(freshness=freshness_mode) + fetch_serper_channels(
        serper_key, freshness=freshness_mode
    )

    conn = get_db()
    cur = conn.cursor()

    saved = 0
    rejected = 0
    skipped_duplicates = 0
    notes = []

    for item in candidates:
        post_url = item.get("url", "").strip()
        ext_id = item.get("external_id", "").strip()

        # 2. Strict Duplicate Lead Prevention BEFORE calling Gemini API (preserves API quota)
        cur.execute(
            "SELECT id FROM leads WHERE external_id = ? OR (url = ? AND url != '' AND url != '#')",
            (ext_id, post_url),
        )
        if cur.fetchone():
            skipped_duplicates += 1
            continue

        score = 5
        service_category = "Web/App Dev"
        client_region = "Global"
        pain_point_summary = item.get("title", "")
        suggested_pitch = ""

        # 3. Qualify with Gemini Failover Pool using strict structured JSON
        if key_pool:
            try:
                eval_res = score_and_draft(item, agent_prompt)
                time.sleep(0.35)

                score = eval_res.get("score", 1)
                service_category = eval_res.get("service_category", "Web/App Dev")
                client_region = eval_res.get("client_region", "Global")
                pain_point_summary = eval_res.get("pain_point_summary", item.get("title", ""))
                suggested_pitch = eval_res.get("suggested_codexplus_pitch", "")

                if score < min_keep_score:
                    rejected += 1
                    continue
            except Exception as exc:
                notes.append(f"Gemini ({ext_id}): {exc}")
        else:
            notes.append("No Gemini API keys configured.")

        # 4. Save Qualified Lead to Database
        cur.execute(
            """
            INSERT INTO leads (
                external_id, platform, author, title, raw_text, url,
                score, summary, suggested_reply, service_category,
                client_region, pain_point_summary, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                ext_id,
                item["platform"],
                item["author"],
                item["title"],
                item["raw_text"],
                post_url,
                score,
                pain_point_summary,
                suggested_pitch,
                service_category,
                client_region,
                pain_point_summary,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        saved += 1

    conn.commit()
    conn.close()

    summary_note = f"Freshness: {freshness_mode}; Fetched: {len(candidates)}; Saved: {saved}; Rejected: {rejected}; Skipped Duplicates: {skipped_duplicates}"
    if notes:
        summary_note += f"; Notes: {'; '.join(notes[:2])}"

    return {
        "fetched": len(candidates),
        "saved": saved,
        "rejected": rejected,
        "skipped_duplicates": skipped_duplicates,
        "notes": summary_note,
    }
