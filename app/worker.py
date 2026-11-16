import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.db import get_settings_dict, pool

logger = logging.getLogger("lead_worker")

SYSTEM_INSTRUCTION = (
    "You are a senior technical solutions architect for a full-stack software house "
    "specializing in SaaS MVPs, custom web/mobile apps, ERPs/CRMs, and AI integrations. "
    "Evaluate the social media post for real B2B buying intent. Score 1-3 for job seekers, "
    "spam, or vague ideas; 4-6 for early brainstorming; 7-10 for founders or businesses "
    "actively seeking developers, agencies, or technical quotes. If score is 7 or higher, "
    "write a concise, value-first reply (under 90 words) that directly addresses their "
    "technical architecture challenge first, mentions relevant delivery experience subtly, "
    "and ends with a low-friction question. Never use generic agency buzzwords or pushy links."
)


def fetch_reddit_leads(query: str, twelve_hours_ago: float) -> list[dict[str, Any]]:
    client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
    user_agent = os.getenv("REDDIT_USER_AGENT", "SoftwareHouseLeadEngine/1.0").strip()

    if not client_id or not client_secret:
        logger.info("Reddit credentials not configured; skipping Reddit fetch.")
        return []

    leads: list[dict[str, Any]] = []
    with httpx.Client(timeout=20.0) as client:
        token_resp = client.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": user_agent},
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get("access_token")
        if not access_token:
            return []

        search_resp = client.get(
            "https://oauth.reddit.com/search",
            params={"q": query, "sort": "new", "limit": 25, "t": "day"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": user_agent,
            },
        )
        search_resp.raise_for_status()
        children = search_resp.json().get("data", {}).get("children", [])

        for child in children:
            data = child.get("data", {})
            post_id = data.get("id")
            if not post_id:
                continue
            created_utc = float(data.get("created_utc") or time.time())
            if created_utc < twelve_hours_ago:
                continue

            raw_text = f"{data.get('title', '')}\n\n{data.get('selftext', '')}".strip()
            if not raw_text:
                continue

            permalink = data.get("permalink")
            post_url = (
                f"https://www.reddit.com{permalink}"
                if permalink
                else f"https://www.reddit.com/comments/{post_id}"
            )
            author = data.get("author")
            leads.append(
                {
                    "platform": "reddit",
                    "platform_post_id": f"reddit_{post_id}",
                    "author_handle": f"u/{author}" if author else "unknown",
                    "post_url": post_url,
                    "raw_text": raw_text[:4000],
                }
            )
    return leads


def fetch_x_leads(query: str, twelve_hours_ago: float) -> list[dict[str, Any]]:
    bearer = os.getenv("X_BEARER_TOKEN", "").strip()
    if not bearer:
        logger.info("X_BEARER_TOKEN not configured; skipping X fetch.")
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
    if not api_key:
        return {
            "intent_score": 0,
            "is_qualified": False,
            "project_category": "other",
            "pain_point_summary": "GEMINI_API_KEY not configured",
            "drafted_reply": "",
        }

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
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
                            "erp_crm",
                            "ai_automation",
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

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, headers={"x-goog-api-key": api_key}, json=body)
        resp.raise_for_status()
        data = resp.json()

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
    if not webhook_url:
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
        candidates.extend(fetch_reddit_leads(reddit_query, twelve_hours_ago))
    except Exception as exc:
        logger.exception("Reddit fetch failed")
        errors.append(f"Reddit: {exc}")

    try:
        candidates.extend(fetch_x_leads(x_query, twelve_hours_ago))
    except Exception as exc:
        logger.exception("X fetch failed")
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
