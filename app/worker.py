# ... existing code ...
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
# ... existing code ...
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
# ... existing code ...
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
# ... existing code ...