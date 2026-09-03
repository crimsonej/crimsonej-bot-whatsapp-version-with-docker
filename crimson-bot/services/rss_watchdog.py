"""
services/rss_watchdog.py
========================
RSS Feed & News Watchdog.
Parses RSS/Atom news feeds for automated topic monitoring.
"""

import logging
from typing import Dict, Any, List

log = logging.getLogger("crimson")

# Common feed mappings for quick topic lookup
POPULAR_FEEDS = {
    "tech": "https://feeds.feedburner.com/TechCrunch/",
    "ai": "https://hnrss.org/newest?q=AI",
    "crypto": "https://cointelegraph.com/rss",
    "news": "http://feeds.bbci.co.uk/news/rss.xml",
    "finance": "https://search.cnbc.com/rs/search/combinedradios/rss.xml?partnerId=2000&keywords=markets"
}

def fetch_rss_feed(feed_url_or_topic: str, max_items: int = 5) -> Dict[str, Any]:
    """
    Fetch and parse an RSS feed URL or preset topic.
    """
    if not feed_url_or_topic:
        return {"ok": False, "items": [], "error": "Feed URL or topic required"}

    target = feed_url_or_topic.strip().lower()
    url = POPULAR_FEEDS.get(target, feed_url_or_topic.strip())
    
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        import feedparser
        feed = feedparser.parse(url)

        if feed.bozo and not feed.entries:
            return {"ok": False, "items": [], "error": f"Failed to parse RSS feed at {url}"}

        items = []
        for entry in feed.entries[:max_items]:
            title = entry.get("title", "No Title")
            link = entry.get("link", "")
            published = entry.get("published", entry.get("updated", ""))
            summary = entry.get("summary", entry.get("description", ""))
            
            # Clean HTML from summary
            import re
            clean_summary = re.sub(r"<[^>]+>", "", summary).strip()[:300]

            items.append({
                "title": title,
                "link": link,
                "published": published,
                "summary": clean_summary
            })

        feed_title = feed.feed.get("title", url)
        log.info(f"[RSS] Extracted {len(items)} items from feed: {feed_title}")
        return {
            "ok": True,
            "feed_title": feed_title,
            "feed_url": url,
            "count": len(items),
            "items": items,
            "error": None
        }
    except Exception as e:
        log.warning(f"[RSS] Parsing failed for {url}: {e}")
        return {
            "ok": False,
            "items": [],
            "error": f"RSS feed error: {str(e)}"
        }
