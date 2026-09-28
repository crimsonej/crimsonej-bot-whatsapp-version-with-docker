"""Ephemeral static web previews served by the existing Flask service."""

from __future__ import annotations

import os
import math
import ipaddress
import re
import secrets
import subprocess
import threading
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse


MAX_HTML_BYTES = 1024 * 1024
MAX_ACTIVE_APPS = 8
MAX_ACTIVE_BYTES = 4 * 1024 * 1024
MAX_TTL_SECONDS = 6 * 60 * 60
_apps: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def _public_base_url() -> str:
    base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not base:
        domain = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip().strip("/")
        if domain:
            base = domain if "://" in domain else f"https://{domain}"
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or not parsed.hostname
        or parsed.hostname.endswith((".local", ".internal"))
    ):
        return ""
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global:
            return ""
    except ValueError:
        pass
    return base


def _purge_expired(now: float | None = None) -> None:
    now = time.time() if now is None else now
    expired = [token for token, (_, expires_at) in _apps.items() if expires_at <= now]
    for token in expired:
        _apps.pop(token, None)


def publish_static_preview(html: str, uptime_hours: float = 6) -> dict:
    """Publish isolated static HTML with a random, expiring bearer URL."""
    if not isinstance(html, str) or not html.strip():
        return {"ok": False, "error": "HTML content is required."}
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        return {"ok": False, "error": "Preview exceeds the 256 KiB limit."}
    try:
        hours = float(uptime_hours)
    except (TypeError, ValueError):
        hours = 6
    if not math.isfinite(hours):
        hours = 6
    ttl = min(MAX_TTL_SECONDS, max(60, int(hours * 3600)))
    base_url = _public_base_url()
    if not base_url:
        return {"ok": False, "error": "Configure PUBLIC_BASE_URL or Railway's public domain before publishing previews."}

    token = secrets.token_urlsafe(24)
    expires_at = time.time() + ttl
    with _lock:
        _purge_expired()
        active_bytes = sum(len(content.encode("utf-8")) for content, _ in _apps.values())
        if len(_apps) >= MAX_ACTIVE_APPS or active_bytes + len(html.encode("utf-8")) > MAX_ACTIVE_BYTES:
            return {"ok": False, "error": "Temporary preview capacity is full; revoke an existing preview and retry."}
        _apps[token] = (html, expires_at)
    return {
        "ok": True,
        "url": f"{base_url}/preview/{token}",
        "token": token,
        "expires_at": int(expires_at),
        "ttl_seconds": ttl,
    }


def build_and_publish_react_preview(
    app_name: str,
    jsx: str,
    css: str = "",
    uptime_hours: float = 6,
) -> dict:
    """Build a fixed React/Vite client bundle inside the container, then publish static HTML."""
    if not isinstance(jsx, str) or not jsx.strip() or len(jsx.encode("utf-8")) > 96 * 1024:
        return {"ok": False, "error": "React source must be between 1 byte and 96 KiB."}
    if not isinstance(css, str) or len(css.encode("utf-8")) > 48 * 1024:
        return {"ok": False, "error": "CSS must be no larger than 48 KiB."}

    runtime = Path(os.getenv("PREVIEW_RUNTIME_DIR") or "/app/preview-runtime")
    vite = runtime / "node_modules" / ".bin" / "vite"
    if not vite.is_file():
        return {"ok": False, "error": "Pinned React/Vite runtime is missing from the bot image; rebuild the image first."}

    title = re.sub(r"[<>\x00-\x1f]", "", str(app_name or "Temporary app"))[:100] or "Temporary app"
    try:
        with tempfile.TemporaryDirectory(prefix="crimson-preview-") as temp_dir:
            project = Path(temp_dir)
            (project / "src").mkdir()
            (project / "node_modules").symlink_to(runtime / "node_modules", target_is_directory=True)
            (project / "package.json").write_text(
                '{"private":true,"type":"module","dependencies":{"react":"19.1.0","react-dom":"19.1.0"}}',
                encoding="utf-8",
            )
            (project / "index.html").write_text(
                "<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>"
                + title.replace("&", "&amp;").replace('"', "&quot;")
                + "</title></head><body><div id=\"root\"></div><script type=\"module\" src=\"/src/main.jsx\"></script></body></html>",
                encoding="utf-8",
            )
            (project / "src" / "main.jsx").write_text(
                'import React from "react"; import { createRoot } from "react-dom/client"; import "./style.css";\n'
                + jsx
                + '\nconst root = createRoot(document.getElementById("root")); root.render(React.createElement(App));\n',
                encoding="utf-8",
            )
            (project / "src" / "style.css").write_text(css, encoding="utf-8")
            (project / "vite.config.js").write_text(
                'import { defineConfig } from "vite"; export default defineConfig({base:"/", build:{assetsInlineLimit:1000000000, outDir:"dist", emptyOutDir:true}});',
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(vite), "build", "--config", str(project / "vite.config.js")],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if result.returncode != 0:
                return {"ok": False, "error": (result.stderr or result.stdout)[-3000:]}

            output_dir = project / "dist"
            html_path = output_dir / "index.html"
            html = html_path.read_text(encoding="utf-8")
            html = re.sub(
                r'<script type="module" crossorigin src="([^"]+)"></script>',
                lambda match: '<script type="module">' + Path(output_dir / match.group(1).lstrip("/")).read_text(encoding="utf-8") + "</script>"
                if (output_dir / match.group(1).lstrip("/")).is_file() else match.group(0),
                html,
            )
            for match in list(re.finditer(r'<link rel="stylesheet" crossorigin href="([^"]+)">', html)):
                asset_path = output_dir / match.group(1).lstrip("/")
                if asset_path.is_file():
                    css_text = asset_path.read_text(encoding="utf-8")
                    html = html.replace(match.group(0), "<style>" + css_text + "</style>")
            for asset in output_dir.rglob("*"):
                if asset.is_file() and asset != html_path:
                    relative = asset.relative_to(output_dir).as_posix()
                    data_uri = "data:application/octet-stream;base64," + __import__("base64").b64encode(asset.read_bytes()).decode("ascii")
                    html = html.replace("/" + relative, data_uri)
            return publish_static_preview(html, uptime_hours)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "The app build exceeded 90 seconds."}
    except OSError as exc:
        return {"ok": False, "error": f"Could not start the pinned app builder: {exc}"}


def get_preview(token: str) -> str | None:
    """Look up a preview without revealing whether malformed tokens exist."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", token or ""):
        return None
    with _lock:
        _purge_expired()
        entry = _apps.get(token)
        return entry[0] if entry else None


def revoke_static_preview(token: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", token or ""):
        return False
    with _lock:
        return _apps.pop(token, None) is not None


def active_preview_count() -> int:
    with _lock:
        _purge_expired()
        return len(_apps)