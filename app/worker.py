import hashlib
import html
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

from app.db import get_settings_dict, pool

logger = logging.getLogger("lead_worker")

GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-2.5-flash",
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
    if "news.ycombinator.com" in lower:
        return "hackernews"
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
    elif platform == "hackernews":
        match = re.search(r"id=(\d+)", url)
        if match:
            return f"hn_{match.group(1)}"
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
    if platform == "hackernews":
        return "hackernews"
    return platform


def fetch_fresh_reddit_arctic_shift(cutoff_hours: float = 6.0) -> list[dict[str, Any]]:
    """Fetch recent Reddit hiring posts via Arctic Shift public archive."""
    leads: list[dict[str, Any]] = []
    cutoff_time = time.time() - (cutoff_hours * 3600)

    with httpx.Client(timeout=15.0) as client:
        for sub in HIRING_SUBREDDITS:
            try:
                resp = client.get(
                    "https://arctic-shift.photon-reddit.com/api/posts/search",
                    params={"subreddit": sub, "limit": 20, "sort": "desc"},
                )
                if resp.status_code != 200:
                    continue
                for post in resp.json().get("data", []):
                    post_id = post.get("id")
                    title = (post.get("title") or "").strip()
                    selftext = (post.get("selftext") or "").strip()
                    created_utc = float(post.get("created_utc") or 0)
                    num_comments = int(post.get("num_comments") or 0)

                    if not post_id or created_utc < cutoff_time:
                        continue
                    if num_comments > 5:
                        continue  # Skip heavily replied posts

                    lower_title = title.lower()
                    if "[for hire]" in lower_title or "for hire" in lower_title:
                        continue

                    lower_content = (title + " " + selftext).lower()
                    if sub != "arduino":
                        has_hiring = "[hiring]" in lower_title or "hiring" in lower_title
                        has_budget = "budget" in lower_content or "$" in lower_content or "paying" in lower_content
                        if not (has_hiring or has_budget):
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
                "tbs": "qdr:d",  # Past 24 hours
            },
        )
        if resp.status_code >= 400:
            logger.warning("Serper error %s: %s", resp.status_code, resp.text[:200])
            return []
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


def _fetch_reddit_rss(client: httpx.Client, query: str, user_agent: str, cutoff_time: float) -> list[dict[str, Any]]:
    leads: list[dict[str, Any]] = []
    try:
        resp = client.get(
            "https://www.reddit.com/search.rss",
            params={"q": query, "sort": "new", "t": "day"},
            headers={"User-Agent": user_agent},
        )
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        for entry in root.findall("atom:entry", ns):
            entry_id = (entry.findtext("atom:id", "", ns) or "").strip()
            title = (entry.findtext("atom:title", "", ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            post_url = link_el.attrib.get("href", "") if link_el is not None else ""
            author_name = (entry.findtext("atom:author/atom:name", "", ns) or "unknown").strip()
            updated_str = (entry.findtext("atom:updated", "", ns) or "").strip()
            content_html = entry.findtext("atom:content", "", ns) or ""

            if updated_str:
                try:
                    updated_ts = datetime.fromisoformat(updated_str.replace("Z", "+00:00")).timestamp()
                    if updated_ts < cutoff_time:
                        continue
                except ValueError:
                    pass

            clean_body = re.sub(r"<[^>]+>", " ", html.unescape(content_html))
            clean_body = re.sub(r"\s+", " ", clean_body).strip()
            raw_text = f"{title}\n\n{clean_body}".strip()
            if not raw_text:
                continue

            post_id = entry_id.split("_")[-1] if "_" in entry_id else entry_id.rsplit("/", 1)[-1]
            if not post_id:
                continue

            leads.append(
                {
                    "platform": "reddit",
                    "platform_post_id": f"reddit_{post_id}",
                    "author_handle": author_name if author_name.startswith("u/") or author_name.startswith("/u/") else f"u/{author_name}",
                    "post_url": post_url or f"https://www.reddit.com/comments/{post_id}",
                    "raw_text": raw_text[:4000],
                }
            )
    except Exception as exc:
        logger.info("Reddit RSS fallback skipped: %s", exc)
    return leads


def fetch_reddit_leads(query: str, twelve_hours_ago: float) -> list[dict[str, Any]]:
    client_query = (
        '("[Hiring]" OR "looking to hire" OR "need someone to build" OR "need a developer" '
        'OR "need a website" OR "need an app" OR "need a graphic designer" OR "need SEO" OR "Arduino project") '
        '("budget" OR "$" OR "paying" OR "quote") '
        '-"[For Hire]" -"for hire" -"I am a" -"looking for work" -"full-time" -"internship"'
    )
    leads = _serper_search(f"site:reddit.com {client_query}", num=10)
    if leads:
        return leads

    # Fallback if Serper is not configured: Reddit RSS
    user_agent = os.getenv("REDDIT_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36").strip()
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        return _fetch_reddit_rss(client, query or client_query, user_agent, twelve_hours_ago)


def fetch_social_leads_via_serper(query: str) -> list[dict[str, Any]]:
    site_filter = "(site:x.com OR site:threads.net OR site:facebook.com OR site:t.me)"
    client_query = (
        '("looking to hire" OR "need a developer" OR "need a website" OR "need an app" '
        'OR "need a graphic designer" OR "need SEO" OR "need social media marketing") '
        '("budget" OR "$" OR "paying" OR "DM me") '
        '-"for hire" -"available for work" -"portfolio" -"full-time" -"internship"'
    )
    return _serper_search(f"{site_filter} {client_query}", num=15)


def fetch_hackernews_leads(cutoff_hours: float = 12.0) -> list[dict[str, Any]]:
    leads: list[dict[str, Any]] = []
    cutoff_time = time.time() - (cutoff_hours * 3600)
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "query": "looking to hire OR need developer OR need MVP OR software agency OR build SaaS",
                "tags": "(story,ask_hn)",
                "numericFilters": f"created_at_i>{int(cutoff_time)},num_comments<=5",
                "hitsPerPage": 15,
            },
        )
        if resp.status_code != 200:
            return []
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
            model_succeeded = False
            for attempt in range(2):
                try:
                    resp = client.post(url, headers={"x-goog-api-key": api_key}, json=body)
                    if resp.status_code == 200:
                        data = resp.json()
                        model_succeeded = True
                        break
                    last_error = f"{model_name} HTTP {resp.status_code}: {resp.text[:200]}"
                    if resp.status_code in (429, 500, 502, 503, 504):
                        time.sleep(2)
                        continue
                    # 400 or 404 indicates model incompatibility; try next model
                    break
                except httpx.RequestError as req_err:
                    last_error = f"{model_name} network error: {req_err}"
                    time.sleep(1)
                    continue

            if model_succeeded and data is not None:
                break

    if data is None:
        raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")

    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {data}")

    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        raise RuntimeError(f"Gemini candidate content has no parts: {candidates[0]}")

    raw_text = parts[0].get("text", "{}").strip()
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE)
    raw_text = re.sub(r"\s*```$", "", raw_text)

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as jde:
        raise RuntimeError(f"Failed to parse Gemini JSON: {jde}. Raw: {raw_text[:200]}")

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
    if len(content) > 1950:
        content = content[:1940] + "\n```\n..."

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
        candidates.extend(fetch_fresh_reddit_arctic_shift(cutoff_hours=6.0))
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
        candidates.extend(fetch_hackernews_leads(cutoff_hours=12.0))
    except Exception as exc:
        logger.exception("HackerNews fetch failed")
        errors.append(f"HackerNews: {exc}")

    try:
        candidates.extend(fetch_x_leads(x_query, twelve_hours_ago))
    except Exception as exc:
        logger.exception("X API fetch failed")
        errors.append(f"X: {exc}")

    # Deduplicate candidates in memory by platform_post_id
    seen_ids: set[str] = set()
    deduped_candidates: list[dict[str, Any]] = []
    for c in candidates:
        pid = c.get("platform_post_id")
        if pid and pid not in seen_ids:
            seen_ids.add(pid)
            deduped_candidates.append(c)

    saved_count = 0
    qualified_count = 0

    for lead in deduped_candidates:
        # Check if already processed
        with pool.connection() as conn:
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
        with pool.connection() as conn:
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
        "fetched": len(deduped_candidates),
        "saved": saved_count,
        "qualified": qualified_count,
        "errors": errors,
    }
