"""
services/web_reader.py
======================
Web Page Content Reader & Article Extractor.
Extracts main article text, headers, and clean readable content from any URL.
"""

import logging
import re
from typing import Dict, Any, Optional

log = logging.getLogger("crimson")

def fetch_url_content(url: str, max_chars: int = 15000) -> Dict[str, Any]:
    """
    Fetches the content of a web page URL and extracts the main readable text.
    
    Args:
        url: The web URL to fetch.
        max_chars: Maximum characters of extracted text to return.
        
    Returns:
        Dict with keys: ok (bool), title (str), domain (str), text (str), error (str/None)
    """
    if not url or not isinstance(url, str):
        return {"ok": False, "title": "", "domain": "", "text": "", "error": "Invalid URL"}

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    from urllib.parse import urlparse
    domain = urlparse(url).netloc

    # Method 1: Trafilatura (Best article body extractor)
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            extracted = trafilatura.extract(
                downloaded,
                include_links=False,
                include_images=False,
                include_tables=True,
                output_format="txt"
            )
            title_extracted = trafilatura.extract_metadata(downloaded)
            page_title = title_extracted.title if (title_extracted and title_extracted.title) else domain

            if extracted and len(extracted.strip()) > 100:
                text_clean = extracted.strip()[:max_chars]
                log.info(f"[WebReader] Trafilatura extracted {len(text_clean)} chars from {domain}")
                return {
                    "ok": True,
                    "title": page_title,
                    "domain": domain,
                    "url": url,
                    "text": text_clean,
                    "error": None
                }
    except Exception as e:
        log.warning(f"[WebReader] Trafilatura failed for {url}: {e}")

    # Method 2: Fallback with httpx + BeautifulSoup4
    try:
        import httpx
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"
        }
        
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")
        
        # Remove script, style, nav, footer, ads
        for element in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            element.decompose()

        page_title = soup.title.string.strip() if (soup.title and soup.title.string) else domain
        
        # Extract text from paragraphs and headings
        paragraphs = []
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
            t = tag.get_text().strip()
            if t and len(t) > 20:
                paragraphs.append(t)

        full_text = "\n\n".join(paragraphs)
        if full_text.strip():
            text_clean = full_text.strip()[:max_chars]
            log.info(f"[WebReader] BS4 fallback extracted {len(text_clean)} chars from {domain}")
            return {
                "ok": True,
                "title": page_title,
                "domain": domain,
                "url": url,
                "text": text_clean,
                "error": None
            }
    except Exception as e:
        log.error(f"[WebReader] BS4 fallback failed for {url}: {e}")

    return {
        "ok": False,
        "title": domain,
        "domain": domain,
        "url": url,
        "text": "",
        "error": f"Failed to extract text content from {url}"
    }
