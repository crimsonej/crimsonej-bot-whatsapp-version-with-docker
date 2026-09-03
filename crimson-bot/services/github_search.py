"""
services/github_search.py
==========================
GitHub Code & Repository Search Service.
Uses public GitHub API to search repositories, descriptions, and code information.
"""

import logging
from typing import Dict, Any, List
import httpx

log = logging.getLogger("crimson")

GITHUB_HEADERS = {
    "User-Agent": "Crimsonej-Bot-Engine/2.0",
    "Accept": "application/vnd.github.v3+json"
}

def search_github(query: str, search_type: str = "repositories", limit: int = 5) -> Dict[str, Any]:
    """
    Search GitHub repositories or topics.
    
    Args:
        query: Search keywords
        search_type: 'repositories' or 'code'
        limit: Max results (default 5)
    """
    if not query:
        return {"ok": False, "results": [], "error": "Query required"}

    query_str = query.strip()
    url = "https://api.github.com/search/repositories"
    params = {"q": query_str, "sort": "stars", "order": "desc", "per_page": limit}

    try:
        with httpx.Client(timeout=10.0, headers=GITHUB_HEADERS, follow_redirects=True) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        items = data.get("items", [])
        results = []

        for item in items:
            name = item.get("full_name", "")
            desc = item.get("description") or "No description provided."
            stars = item.get("stargazers_count", 0)
            lang = item.get("language") or "N/A"
            repo_url = item.get("html_url", "")
            updated = item.get("updated_at", "")[:10]

            results.append({
                "name": name,
                "description": desc,
                "stars": stars,
                "language": lang,
                "url": repo_url,
                "last_updated": updated
            })

        log.info(f"[GitHub] Found {len(results)} repositories for query '{query_str}'")
        return {
            "ok": True,
            "query": query_str,
            "count": len(results),
            "results": results,
            "error": None
        }
    except Exception as e:
        log.warning(f"[GitHub] Search failed for '{query_str}': {e}")
        return {
            "ok": False,
            "query": query_str,
            "results": [],
            "error": f"GitHub search error: {str(e)}"
        }
