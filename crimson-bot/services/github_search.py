"""
services/github_search.py
=========================
GitHub Code & Repository Search Service + Write Operations.
Uses public GitHub API to search repositories and write files (requires GITHUB_TOKEN with repo scope).
"""

import base64
import logging
import os
from typing import Dict, Any, List, Optional
import httpx

log = logging.getLogger("crimson")

GITHUB_HEADERS = {
    "User-Agent": "Crimsonej-Bot-Engine/2.0",
    "Accept": "application/vnd.github.v3+json"
}

def _get_auth_headers() -> Dict[str, str]:
    """Get headers with auth if token available."""
    headers = GITHUB_HEADERS.copy()
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


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
        with httpx.Client(timeout=10.0, headers=_get_auth_headers(), follow_redirects=True) as client:
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
            "count": 0,
            "results": [],
            "error": f"GitHub search error: {str(e)}"
        }


def _github_api_request(method: str, url: str, **kwargs) -> Dict[str, Any]:
    """Make authenticated GitHub API request."""
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        return {"ok": False, "error": "GITHUB_TOKEN not configured. Add to .env with repo scope."}
    
    headers = _get_auth_headers()
    try:
        with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
            resp = client.request(method, url, **kwargs)
            resp.raise_for_status()
            return {"ok": True, "data": resp.json()}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"ok": False, "error": f"GitHub API error: {str(e)}"}


def github_create_file(repo: str, path: str, content: str, message: str, branch: str = "main") -> Dict[str, Any]:
    """
    Create a new file in a GitHub repository.
    
    Args:
        repo: Repository in format 'owner/repo'
        path: File path in repo
        content: File content (will be base64 encoded)
        message: Commit message
        branch: Branch name (default: main)
    """
    if not repo or not path or not content:
        return {"ok": False, "error": "repo, path, and content are required"}
    
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    
    payload = {
        "message": message,
        "content": encoded_content,
        "branch": branch
    }
    
    result = _github_api_request("PUT", url, json=payload)
    if result["ok"]:
        log.info(f"[GitHub] Created file {path} in {repo}")
        return {"ok": True, "commit": result["data"].get("commit"), "content": result["data"].get("content")}
    return result


def github_update_file(repo: str, path: str, content: str, message: str, sha: str, branch: str = "main") -> Dict[str, Any]:
    """
    Update an existing file in a GitHub repository.
    
    Args:
        repo: Repository in format 'owner/repo'
        path: File path in repo
        content: New file content
        message: Commit message
        sha: Current file SHA (required for updates)
        branch: Branch name (default: main)
    """
    if not repo or not path or not content or not sha:
        return {"ok": False, "error": "repo, path, content, and sha are required"}
    
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    
    payload = {
        "message": message,
        "content": encoded_content,
        "sha": sha,
        "branch": branch
    }
    
    result = _github_api_request("PUT", url, json=payload)
    if result["ok"]:
        log.info(f"[GitHub] Updated file {path} in {repo}")
        return {"ok": True, "commit": result["data"].get("commit"), "content": result["data"].get("content")}
    return result


def github_get_file(repo: str, path: str, branch: str = "main") -> Dict[str, Any]:
    """
    Get file content and SHA from a GitHub repository.
    
    Args:
        repo: Repository in format 'owner/repo'
        path: File path in repo
        branch: Branch name (default: main)
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    params = {"ref": branch}
    
    result = _github_api_request("GET", url, params=params)
    if result["ok"]:
        data = result["data"]
        if isinstance(data, list):
            return {"ok": False, "error": f"Path '{path}' is a directory, not a file"}
        content = base64.b64decode(data.get("content", "")).decode("utf-8") if data.get("content") else ""
        return {
            "ok": True,
            "content": content,
            "sha": data.get("sha"),
            "path": data.get("path"),
            "size": data.get("size")
        }
    return result


def github_upsert_file(repo: str, path: str, content: str, message: str, branch: str = "main") -> Dict[str, Any]:
    """
    Create or update a file (upsert). Checks if file exists first.
    """
    # Try to get existing file
    existing = github_get_file(repo, path, branch)
    if existing["ok"]:
        # File exists, update it
        return github_update_file(repo, path, content, message, existing["sha"], branch)
    else:
        # File doesn't exist, create it
        if "404" in existing.get("error", "") or "Not Found" in existing.get("error", ""):
            return github_create_file(repo, path, content, message, branch)
        return existing


def github_list_files(repo: str, path: str = "", branch: str = "main") -> Dict[str, Any]:
    """
    List files in a repository directory.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    params = {"ref": branch}
    
    result = _github_api_request("GET", url, params=params)
    if result["ok"]:
        data = result["data"]
        if isinstance(data, list):
            files = []
            for item in data:
                files.append({
                    "name": item.get("name"),
                    "path": item.get("path"),
                    "type": item.get("type"),  # file or dir
                    "size": item.get("size"),
                    "sha": item.get("sha")
                })
            return {"ok": True, "files": files}
        return {"ok": False, "error": "Unexpected response format"}
    return result


def github_commit_changes(repo: str, message: str, changes: List[Dict], branch: str = "main") -> Dict[str, Any]:
    """
    Commit multiple file changes in a single commit using GitHub's Git Database API.
    This is more complex - for now, use individual create/update calls.
    """
    return {"ok": False, "error": "Multi-file commit not yet implemented. Use individual create/update calls."}