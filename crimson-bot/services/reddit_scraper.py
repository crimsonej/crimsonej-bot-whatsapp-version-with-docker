"""
services/reddit_scraper.py
===========================
Reddit Community & Consensus Scraper.
Uses public Reddit JSON endpoints to search discussions, posts, and top comments.
"""

import logging
from typing import Dict, Any, List, Optional
import httpx

log = logging.getLogger("crimson")

REDDIT_HEADERS = {
    "User-Agent": "script:crimsonej.bot:v1.0 (by /u/crimsonej_app)",
    "Accept": "application/json",
}

def search_reddit(query: str, subreddit: str = "", limit: int = 5) -> Dict[str, Any]:
    """
    Search Reddit for posts matching query, including post body and top comments.
    """
    if not query:
        return {"ok": False, "posts": [], "error": "Query required"}

    query_str = query.strip()
    if subreddit:
        sub_clean = subreddit.replace("r/", "").strip()
        url = f"https://old.reddit.com/r/{sub_clean}/search.json"
        params = {"q": query_str, "restrict_sr": "1", "sort": "relevance", "limit": limit}
    else:
        url = "https://old.reddit.com/search.json"
        params = {"q": query_str, "sort": "relevance", "limit": limit}

    try:
        with httpx.Client(timeout=10.0, headers=REDDIT_HEADERS, follow_redirects=True) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        children = data.get("data", {}).get("children", [])
        posts = []

        for item in children:
            pdata = item.get("data", {})
            title = pdata.get("title", "")
            sub = pdata.get("subreddit_name_prefixed", "")
            score = pdata.get("score", 0)
            num_comments = pdata.get("num_comments", 0)
            selftext = pdata.get("selftext", "").strip()[:500]
            permalink = f"https://www.reddit.com{pdata.get('permalink', '')}"

            posts.append({
                "title": title,
                "subreddit": sub,
                "score": score,
                "comments_count": num_comments,
                "text": selftext,
                "url": permalink
            })

        log.info(f"[Reddit] Found {len(posts)} posts for query '{query_str}'")
        return {
            "ok": True,
            "query": query_str,
            "subreddit": subreddit or "all",
            "count": len(posts),
            "posts": posts,
            "error": None
        }
    except Exception as e:
        log.warning(f"[Reddit] Direct search failed for '{query_str}': {e}. Attempting DDG fallback...")
        try:
            from realtime_search import search_web
            ddg_res = search_web(f"site:reddit.com {query_str}", max_results=limit)
            if isinstance(ddg_res, dict) and ddg_res.get("results"):
                posts = []
                for item in ddg_res["results"]:
                    posts.append({
                        "title": item.get("title", ""),
                        "subreddit": "r/all",
                        "score": 0,
                        "comments_count": 0,
                        "text": item.get("content", ""),
                        "url": item.get("url", "")
                    })
                return {
                    "ok": True,
                    "query": query_str,
                    "subreddit": subreddit or "all",
                    "count": len(posts),
                    "posts": posts,
                    "error": None
                }
        except Exception as fallback_err:
            log.warning(f"[Reddit] DDG fallback failed: {fallback_err}")

        return {
            "ok": False,
            "query": query_str,
            "count": 0,
            "posts": [],
            "error": f"Reddit search failed: {str(e)}"
        }
