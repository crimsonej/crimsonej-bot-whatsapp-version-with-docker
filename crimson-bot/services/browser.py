"""Short-lived, public-URL-only headless browser for JS-rendered pages."""

from __future__ import annotations

import os
import shutil
from urllib.parse import urlparse


def fetch_rendered_page(url: str, max_chars: int = 12_000) -> dict:
    from services.web_reader import _public_url

    allowed, error = _public_url(url)
    if not allowed:
        return {"ok": False, "title": "", "text": "", "error": error}

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "title": "", "text": "", "error": "Playwright is not installed in the bot image."}

    executable = os.getenv("CHROME_BIN") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not executable:
        return {"ok": False, "title": "", "text": "", "error": "Chromium is not installed in the bot image."}

    def route_request(route):
        request_url = route.request.url
        scheme = urlparse(request_url).scheme
        if scheme in {"data", "blob", "about"}:
            route.continue_()
            return
        is_public, _ = _public_url(request_url)
        if is_public:
            route.continue_()
        else:
            route.abort()

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=executable,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-extensions", "--disable-background-networking"],
            )
            try:
                context = browser.new_context(java_script_enabled=True, accept_downloads=False, service_workers="block")
                context.route("**/*", route_request)
                page = context.new_page()
                page.set_default_navigation_timeout(25_000)
                response = page.goto(url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=4_000)
                except PlaywrightTimeout:
                    pass
                title = page.title()[:300]
                text = page.locator("body").inner_text(timeout=3_000).strip()[:max(0, min(max_chars, 30_000))]
                return {
                    "ok": bool(text),
                    "title": title,
                    "text": text,
                    "status": response.status if response else None,
                    "error": None if text else "Rendered page contained no readable text.",
                }
            finally:
                browser.close()
    except Exception as exc:
        return {"ok": False, "title": "", "text": "", "error": f"Browser rendering failed: {type(exc).__name__}"}