import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.db import get_settings_dict, pool

logger = logging.getLogger("lead_worker")

GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
]

HIRING_SUBREDDITS = [
    "forhire",
    "freelance_forhire",
    "DesignJobs",
    "slavelabour",
    "hiring",
    "arduino",
]

SYSTEM_INSTRUCTION = (
    "You are a senior solutions architect at CodeXplus (codexplus.site), a full-service software house "
    "offering mobile & web apps, custom software, graphic design & digital art, social media marketing, "
    "SEO & GEO, and custom Arduino hardware/software on client demand. "
    "Evaluate the post strictly for CLIENT PROJECT ORDERS WITH BUDGETS. "
    "Score 1-2 (is_qualified=false) if the author is a developer, designer, marketer, or agency offering "
    "or advertising their own services ('[For Hire]', 'I build websites', 'DM me for services'), "
    "or if it is a full-time job ad, internship, tutorial, or showcase. "
    "Score 7-10 (is_qualified=true, 8-10 if a budget is stated) ONLY when a client or business is actively "
    "ordering/hiring someone to build an app, website, custom software, graphic design/art, social media marketing, "
    "SEO/GEO, or Arduino project. Include any mentioned budget in pain_point_summary. "
    "If score is 7 or higher, write a concise, helpful reply (under 90 words) from CodeXplus addressing "
    "their project need directly and ending with a low-friction question."
)


def _detect_platform(url: str) -> str:
    lower = url.lower()
    if "reddit.com" in lower:
        return "reddit"
    if "x.com" in lower or "twitter.com" in lower:
        return "x"
    if "threads.net" in lower:
        return "threads"
    if "facebook.com" in lower or "fb.com" in lower:
        return "facebook"
    if "t.me" in lower or "telegram.me" in lower:
        return "telegram"
    if "discord.com" in lower or "discord.gg" in lower or "disboard.org" in lower:
        return "discord"
    if "twist.com" in lower:
        return "twist"
    if "linkedin.com" in lower:
        return "linkedin"
    return "web"


def _extract_post_id(platform: str, url: str) -> str:
    if platform == "reddit":
        match = re.search(r"/comments/([a-z0-9]+)", url, re.IGNORECASE)
        if match:
            return f"reddit_{match.group(1)}"
    elif platform == "x":
        match = re.search(r"/status/(\d+)", url)
        if match:
            return f"x_{match.group(1)}"
    return f"{platform}_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}"


def _extract_handle(platform: str, url: str) -> str:
    if platform == "reddit":
        m = re.search(r"reddit\.com/r/([^/]+)", url, re.IGNORECASE)
        return f"r/{m.group(1)}" if m else "reddit"
    if platform == "x":
        m = re.search(r"(?:x|twitter)\.com/([^/]+)", url, re.IGNORECASE)
        return f"@{m.group(1)}" if m and m.group(1) not in {"i", "search"} else "x_user"
    if platform == "threads":
        m = re.search(r"threads\.net/@?([^/]+)", url, re.IGNORECASE)
        return f"@{m.group(1)}" if m else "threads"
    if platform == "telegram":
        m = re.search(r"t\.me/([^/]+)", url, re.IGNORECASE)
        return f"t.me/{m.group(1)}" if m else "telegram"
    if platform == "discord":
        return "Discord Public"
    if platform == "twist":
        return "Twist Thread"
    if platform == "linkedin":
        m = re.search(r"linkedin\.com/(?:in|posts)/([^/?]+)", url, re.IGNORECASE)
        return m.group(1) if m else "LinkedIn"
    return platform


def fetch_fresh_reddit_arctic_shift() -> list[dict[str, Any]]:
    """Fetch brand-new Reddit posts (<= 60 mins old, <= 2 comments) in near real-time."""
    leads: list[dict[str, Any]] = []
    one_hour_ago = time.time() - 3600

    with httpx.Client(timeout=15.0) as client:
        for sub in HIRING_SUBREDDITS:
            try:
                resp = client.get(
                    "https://arctic-shift.photon-reddit.com/api/posts/search",
                    params={"subreddit": sub, "limit": 15, "sort": "desc"},
                )
                if resp.status_code != 200:
                    continue
                for post in resp.json().get("data", []):
                    post_id = post.get("id")
                    title = (post.get("title") or "").strip()
                    selftext = (post.get("selftext") or "").strip()
                    created_utc = float(post.get("created_utc") or 0)
                    num_comments = int(post.get("num_comments") or 0)

                    if not post_id or created_utc < one_hour_ago:
                        continue
                    if num_comments > 2:
                        continue

                    lower_title = title.lower()
                    if "[for hire]" in lower_title or "for hire" in lower_title:
                        continue

                    if sub != "arduino" and "[hiring]" not in lower_title and "hiring" not in lower_title and "budget" not in (title + selftext).lower():
                        continue

                    permalink = post.get("permalink")
                    post_url = (
                        f"https://www.reddit.com{permalink}"
                        if permalink
                        else f"https://www.reddit.com/r/{sub}/comments/{post_id}"
                    )
                    leads.append(
                        {
                            "platform": "reddit",
                            "platform_post_id": f"reddit_{post_id}",
                            "author_handle": f"u/{post.get('author')}" if post.get("author") else f"r/{sub}",
                            "post_url": post_url,
                            "raw_text": f"[Comments: {num_comments}] {title}\n\n{selftext}".strip()[:4000],
                        }
                    )
            except Exception as exc:
                logger.info("Arctic Shift skip r/%s: %s", sub, exc)
    return leads


def _serper_search(search_query: str, num: int = 10) -> list[dict[str, Any]]:
    serper_key = os.getenv("SERPER_API_KEY", "").strip()
    if not serper_key or serper_key.startswith("your_"):
        return []

    with httpx.Client(timeout=20.0) as client:
        resp = client.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": serper_key,
                "Content-Type": "application/json",
            },
            json={
                "q": search_query,
                "num": num,
                "tbs": "qdr:h",
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Serper {resp.status_code}: {resp.text}")
        data = resp.json()

    leads: list[dict[str, Any]] = []
    for item in data.get("organic", []):
        link = (item.get("link") or "").strip()
        title = (item.get("title") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        if not link or not (title or snippet):
            continue

        platform = _detect_platform(link)
        leads.append(
            {
                "platform": platform,
                "platform_post_id": _extract_post_id(platform, link),
                "author_handle": _extract_handle(platform, link),
                "post_url": link,
                "raw_text": f"{title}\n\n{snippet}".strip()[:4000],
            }
        )
    return leads


def fetch_reddit_leads(query: str, twelve_hours_ago: float) -> list[dict[str, Any]]:
    client_query = (
        '("[Hiring]" OR "looking to hire" OR "need someone to build" OR "need a developer" '
        'OR "need a website" OR "need an app" OR "need a graphic designer" OR "need SEO" OR "Arduino project") '
        '("budget" OR "$" OR "paying" OR "quote") '
        '-"[For Hire]" -"for hire" -"I am a" -"looking for work" -"full-time" -"internship"'
    )
    return _serper_search(f"site:reddit.com {client_query}", num=10)


def fetch_social_leads_via_serper(query: str) -> list[dict[str, Any]]:
    site_filter = (
        "(site:x.com OR site:threads.net OR site:facebook.com OR site:t.me "
        "OR site:discord.com OR site:discord.gg OR site:twist.com OR site:linkedin.com)"
    )
    client_query = (
        '("looking to hire" OR "need a developer" OR "need a website" OR "need an app" '
        'OR "need a graphic designer" OR "need SEO" OR "need social media marketing" OR "Arduino") '
        '("budget" OR "$" OR "paying" OR "DM me" OR "quote") '
        '-"for hire" -"available for work" -"portfolio" -"full-time" -"internship"'
    )
    return _serper_search(f"{site_filter} {client_query}", num=15)


def fetch_hackernews_leads() -> list[dict[str, Any]]:
    leads: list[dict[str, Any]] = []
    one_hour_ago = time.time() - 3600
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "query": "looking to hire OR need developer OR need MVP OR software agency OR build SaaS",
                "tags": "(story,ask_hn)",
                "numericFilters": f"created_at_i>{int(one_hour_ago)},num_comments<=2",
                "hitsPerPage": 10,
            },
        )
        resp.raise_for_status()
        for hit in resp.json().get("hits", []):
            object_id = hit.get("objectID")
            title = (hit.get("title") or "").strip()
            story_text = (hit.get("story_text") or "").strip()
            if not object_id or not (title or story_text):
                continue
            leads.append(
                {
                    "platform": "hackernews",
                    "platform_post_id": f"hn_{object_id}",
                    "author_handle": hit.get("author") or "hn_user",
                    "post_url": f"https://news.ycombinator.com/item?id={object_id}",
                    "raw_text": f"{title}\n\n{story_text}".strip()[:4000],
                }
            )
    return leads


def fetch_x_leads(query: str, twelve_hours_ago: float) -> list[dict[str, Any]]:
    bearer = os.getenv("X_BEARER_TOKEN", "").strip()
    if not bearer or bearer.startswith("your_"):
        return []

    leads: list[dict[str, Any]] = []
    with httpx.Client(timeout=20.0) as client:
        resp = client.get(
            "https://api.twitter.com/2/tweets/search/recent",
            params={
                "query": query,
                "max_results": "25",
                "tweet.fields": "created_at,author_id,public_metrics",
                "expansions": "author_id",
                "user.fields": "username,name",
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )
        resp.raise_for_status()
        payload = resp.json()

        users_map = {
            u["id"]: u["username"]
            for u in payload.get("includes", {}).get("users", [])
            if "id" in u and "username" in u
        }

        for tweet in payload.get("data", []):
            tweet_id = tweet.get("id")
            text = (tweet.get("text") or "").strip()
            if not tweet_id or not text:
                continue

            created_at_str = tweet.get("created_at")
            if created_at_str:
                created_ts = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                ).timestamp()
            else:
                created_ts = time.time()

            if created_ts < twelve_hours_ago:
                continue

            author_id = tweet.get("author_id")
            username = users_map.get(author_id) or author_id or "unknown"
            post_url = (
                f"https://x.com/{username}/status/{tweet_id}"
                if author_id in users_map
                else f"https://x.com/i/web/status/{tweet_id}"
            )
            leads.append(
                {
                    "platform": "x",
                    "platform_post_id": f"x_{tweet_id}",
                    "author_handle": f"@{username}",
                    "post_url": post_url,
                    "raw_text": text[:4000],
                }
            )
    return leads


def score_with_gemini(lead: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key.startswith("your_"):
        raise RuntimeError("GEMINI_API_KEY is not configured in .env")

    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            f"Platform: {lead['platform']}\n"
                            f"Author: {lead['author_handle']}\n"
                            f"Post Content:\n{lead['raw_text']}"
                        )
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "intent_score": {"type": "INTEGER"},
                    "is_qualified": {"type": "BOOLEAN"},
                    "project_category": {
                        "type": "STRING",
                        "enum": [
                            "mvp",
                            "web_app",
                            "mobile_app",
                            "custom_software",
                            "design_art",
                            "marketing_seo_geo",
                            "arduino_iot",
                            "other",
                        ],
                    },
                    "pain_point_summary": {"type": "STRING"},
                    "drafted_reply": {"type": "STRING"},
                },
                "required": [
                    "intent_score",
                    "is_qualified",
                    "project_category",
                    "pain_point_summary",
                    "drafted_reply",
                ],
            },
        },
    }

    last_error = "Unknown Gemini error"
    data = None
    with httpx.Client(timeout=30.0) as client:
        for model_name in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
            for attempt in range(2):
                resp = client.post(url, headers={"x-goog-api-key": api_key}, json=body)
                if resp.status_code == 200:
                    data = resp.json()
                    break
                last_error = f"{model_name} HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2)
                    continue
                if resp.status_code == 404:
                    break
                raise RuntimeError(last_error)
            if data is not None:
                break

    if data is None:
        raise RuntimeError(last_error)

    raw_text = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "{}")
    )
    parsed = json.loads(raw_text)
    return {
        "intent_score": int(parsed.get("intent_score") or 0),
        "is_qualified": bool(parsed.get("is_qualified")),
        "project_category": parsed.get("project_category") or "other",
        "pain_point_summary": parsed.get("pain_point_summary") or "",
        "drafted_reply": parsed.get("drafted_reply") or "",
    }


def send_discord_alert(lead: dict[str, Any]) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url or "your_id" in webhook_url:
        return

    content = (
        f"🚀 **New Qualified Software Lead ({lead['intent_score']}/10 — {lead['project_category'].upper()})**\n"
        f"**Platform:** {lead['platform'].upper()} | **Author:** {lead['author_handle']}\n"
        f"**Pain Point:** {lead['pain_point_summary']}\n"
        f"**Thread:** {lead['post_url']}\n\n"
        f"**Suggested Reply (Review & Send Manually):**\n```\n{lead['drafted_reply']}\n```"
    )
    with httpx.Client(timeout=15.0) as client:
        client.post(webhook_url, json={"content": content}).raise_for_status()


def run_lead_sync() -> dict[str, Any]:
    settings = get_settings_dict()
    reddit_query = settings.get("reddit_query", "")
    x_query = settings.get("x_query", "")
    min_score = int(settings.get("min_intent_score", "7"))
    twelve_hours_ago = time.time() - (12 * 3600)

    errors: list[str] = []
    candidates: list[dict[str, Any]] = []

    try:
        candidates.extend(fetch_fresh_reddit_arctic_shift())
    except Exception as exc:
        logger.exception("Arctic Shift fetch failed")
        errors.append(f"ArcticShift: {exc}")

    try:
        candidates.extend(fetch_reddit_leads(reddit_query, twelve_hours_ago))
    except Exception as exc:
        logger.exception("Reddit Serper fetch failed")
        errors.append(f"Reddit: {exc}")

    try:
        candidates.extend(fetch_social_leads_via_serper(x_query))
    except Exception as exc:
        logger.exception("Social search failed")
        errors.append(f"Social: {exc}")

    try:
        candidates.extend(fetch_hackernews_leads())
    except Exception as exc:
        logger.exception("HackerNews fetch failed")
        errors.append(f"HackerNews: {exc}")

    try:
        candidates.extend(fetch_x_leads(x_query, twelve_hours_ago))
    except Exception as exc:
        logger.exception("X API fetch failed")
        errors.append(f"X: {exc}")

    saved_count = 0
    qualified_count = 0

    with pool.connection() as conn:
        for lead in candidates:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM social_leads WHERE platform_post_id = %s;",
                    (lead["platform_post_id"],),
                )
                if cur.fetchone():
                    continue

            time.sleep(0.5)
            try:
                scored = score_with_gemini(lead)
            except Exception as exc:
                logger.exception("Gemini scoring failed for %s", lead["platform_post_id"])
                errors.append(f"Gemini ({lead['platform_post_id']}): {exc}")
                continue

            full_lead = {**lead, **scored}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO social_leads (
                      platform, platform_post_id, author_handle, post_url,
                      raw_text, intent_score, project_category,
                      pain_point_summary, drafted_reply, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_review')
                    ON CONFLICT (platform_post_id) DO NOTHING
                    RETURNING id;
                    """,
                    (
                        full_lead["platform"],
                        full_lead["platform_post_id"],
                        full_lead["author_handle"],
                        full_lead["post_url"],
                        full_lead["raw_text"],
                        full_lead["intent_score"],
                        full_lead["project_category"],
                        full_lead["pain_point_summary"],
                        full_lead["drafted_reply"],
                    ),
                )
                inserted = cur.fetchone()
            conn.commit()

            if inserted:
                saved_count += 1
                if full_lead["intent_score"] >= min_score:
                    qualified_count += 1
                    try:
                        send_discord_alert(full_lead)
                    except Exception as exc:
                        logger.exception("Discord alert failed")
                        errors.append(f"Discord: {exc}")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fetched": len(candidates),
        "saved": saved_count,
        "qualified": qualified_count,
        "errors": errors,
    }