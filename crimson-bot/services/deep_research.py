"""
services/deep_research.py
==========================
Autonomous Multi-Source Deep Research Agent.
Executes multi-step research across Web, Reddit, and News, compiling detailed reports.
Optionally exports the research report directly as a Word or PDF file attachment!
"""

import logging
import json
from typing import Dict, Any, Optional
from realtime_search import search_web
from services.reddit_scraper import search_reddit
from services.web_reader import fetch_url_content
from services.doc_writer import create_document_file

log = logging.getLogger("crimson")

def run_deep_research(topic: str, export_doc: bool = False, format: str = "pdf") -> Dict[str, Any]:
    """
    Executes autonomous multi-source research on a topic.
    
    Args:
        topic: The research question or topic
        export_doc: Whether to generate a downloadable document (.pdf / .docx)
        format: Export format ('pdf' or 'docx')
        
    Returns:
        Dict with keys: ok (bool), topic (str), report (str), doc_path (str/None), doc_filename (str/None)
    """
    if not topic or not isinstance(topic, str):
        return {"ok": False, "topic": "", "report": "", "error": "Topic required"}

    topic_str = topic.strip()
    log.info(f"[Deep Research] Starting research on: '{topic_str}'")

    # Step 1: Execute Web Search
    web_results = search_web(topic_str, max_results=5)
    
    # Step 2: Execute Reddit Search for community consensus
    reddit_results = search_reddit(topic_str, limit=3)

    # Step 3: Extract top article body if available
    top_article_text = ""
    if isinstance(web_results, dict) and web_results.get("results"):
        top_url = web_results["results"][0].get("url") or web_results["results"][0].get("href")
        if top_url:
            read_res = fetch_url_content(top_url, max_chars=3000)
            if read_res.get("ok"):
                top_article_text = read_res.get("text", "")

    # Step 4: Synthesize Research Findings into Markdown Report
    report_lines = [
        f"# Deep Research Report: {topic_str}",
        "\n## 1. Executive Summary",
        f"This research report synthesizes live intelligence gathered across web search, community discussions, and primary web sources regarding **{topic_str}**.\n",
        "## 2. Key Web Intelligence Findings"
    ]

    if isinstance(web_results, dict) and web_results.get("results"):
        for idx, item in enumerate(web_results["results"][:5], 1):
            title = item.get("title", "")
            snippet = item.get("content", item.get("snippet", item.get("body", "")))
            url = item.get("url", item.get("href", ""))
            report_lines.append(f"{idx}. **{title}**\n   - {snippet}\n   - *Source*: {url}")
    else:
        report_lines.append("No direct web search results returned.")

    if top_article_text:
        report_lines.append("\n## 3. In-Depth Web Page Analysis")
        report_lines.append(f"{top_article_text[:1500]}...\n")

    if reddit_results.get("ok") and reddit_results.get("posts"):
        report_lines.append("## 4. Community Consensus & Social Sentiment (Reddit)")
        for post in reddit_results["posts"]:
            stitle = post.get("title", "")
            sub = post.get("subreddit", "")
            score = post.get("score", 0)
            report_lines.append(f"- **{stitle}** ({sub}, {score} upvotes)")
            if post.get("text"):
                report_lines.append(f"  > {post['text'][:200]}...")

    report_lines.append("\n## 5. Conclusion & Takeaways")
    report_lines.append(f"Research compiled automatically by Crimsonej Deep Research Agent for '{topic_str}'.")

    full_report_md = "\n".join(report_lines)

    # Step 5: Export as Document File if requested
    doc_path = None
    doc_filename = None
    if export_doc:
        clean_title = f"Research_{topic_str[:20].replace(' ', '_')}"
        res = create_document_file(format, f"Research: {topic_str}", full_report_md, clean_title)
        if res:
            doc_path, doc_filename = res
            log.info(f"[Deep Research] Exported document: {doc_filename}")

    return {
        "ok": True,
        "topic": topic_str,
        "report": full_report_md,
        "path": doc_path,
        "filename": doc_filename,
        "file_path": doc_path,
        "file_name": doc_filename,
        "file_format": format if doc_path else None,
        "media_type": "document",
        "error": None
    }


def run_deep_research_task(*, topic: str, export_doc: bool = False,
                           format: str = "pdf", progress=None, **_) -> Dict[str, Any]:
    """Dispatcher entry point for research that may outlive the HTTP request."""
    if progress:
        progress.update(10, "searching the web")
    result = run_deep_research(topic, export_doc=export_doc, format=format)
    if progress:
        progress.update(95, "compiling the report")
    return result
