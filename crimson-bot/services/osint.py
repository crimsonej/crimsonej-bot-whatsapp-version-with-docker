"""Creator-only passive OSINT helpers using public sources and fixed argv."""

from __future__ import annotations

import ipaddress
import base64
import json
import re
import shutil
import subprocess
import tempfile
import os
import sys
import importlib.util
from urllib.parse import urlparse

from realtime_search import search_web


MAX_OUTPUT = 12_000
COMMAND_TIMEOUT = 90
OSINT_BINARIES = ("sherlock", "amass", "dig", "whois", "theHarvester", "maigret", "exiftool", "chromium", "node", "npm", "npx")
_INSTALLABLE = {
    "sherlock": ("sherlock-project==0.15.0", "sherlock", "0.15.0"),
    "maigret": ("maigret==0.6.1", "maigret", "0.6.1"),
    "theHarvester": ("git+https://github.com/laramies/theHarvester.git@4.11.1#egg=theHarvester", "theHarvester", "4.11.1"),
}
_METADATA_FIELDS = (
    "FileName", "FileType", "FileTypeExtension", "MIMEType", "FileSize",
    "CreateDate", "ModifyDate", "MetadataDate", "Make", "Model", "Software",
    "DocumentTitle", "Author", "Creator", "Producer", "PageCount",
    "Orientation", "ImageWidth", "ImageHeight",
)


def osint_environment() -> dict:
    """Report packaged research, browser, and app-build tools in this container."""
    return {
        "tools": {name: shutil.which(name) for name in OSINT_BINARIES},
        "playwright_python": importlib.util.find_spec("playwright") is not None,
    }


def install_osint_dependency(tool: str) -> dict:
    """Install one pinned CLI in this container after creator approval."""
    spec = _INSTALLABLE.get(tool)
    if not spec:
        return {"ok": False, "error": "Only the pinned Sherlock, Maigret, and theHarvester packages can be installed at runtime."}
    package, binary, version = spec
    if shutil.which(binary):
        return {"ok": True, "already_installed": True, "tool": tool}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", package],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Installation of {tool} timed out."}
    except OSError as exc:
        return {"ok": False, "error": f"Could not start package installer: {exc}"}
    if result.returncode != 0:
        return {"ok": False, "error": f"Installation failed: {(result.stderr or result.stdout)[-1000:]}"}
    return {"ok": bool(shutil.which(binary)), "tool": tool, "version": version}


def _valid_domain(value: str) -> str | None:
    host = (urlparse(value).hostname or value).strip().rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = ascii_host.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        return None
    return ascii_host


def _run_public_tool(binary: str, args: list[str], timeout: int = COMMAND_TIMEOUT) -> dict:
    executable = shutil.which(binary)
    if not executable:
        return {"ok": False, "missing": True, "tool": binary, "output": ""}
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "missing": False,
            "tool": binary,
            "output": (result.stdout + "\n" + result.stderr).strip()[:MAX_OUTPUT],
        }
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {"ok": False, "missing": False, "tool": binary, "output": str(output)[:MAX_OUTPUT], "error": "timed out"}
    except OSError as exc:
        return {"ok": False, "missing": False, "tool": binary, "output": "", "error": str(exc)}


def run_public_osint(subject: str, subject_type: str) -> dict:
    """Collect limited public-source signals for an email, username, or domain."""
    subject = (subject or "").strip()
    subject_type = (subject_type or "").lower().strip()
    if not subject or len(subject) > 253:
        return {"ok": False, "error": "Provide a subject no longer than 253 characters."}
    if subject_type not in {"email", "username", "domain"}:
        return {"ok": False, "error": "subject_type must be email, username, or domain."}

    sections: list[str] = []
    missing: set[str] = set()
    urls: list[str] = []

    if subject_type == "email":
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", subject):
            return {"ok": False, "error": "That does not look like a valid email address."}
        domain = _valid_domain(subject.rsplit("@", 1)[1])
        if not domain:
            return {"ok": False, "error": "Email domain is invalid or unsupported."}
        for record_type in ("MX", "TXT"):
            result = _run_public_tool("dig", ["+time=3", "+tries=1", "+short", domain, record_type], timeout=8)
            if result.get("missing"):
                missing.add("dig")
            elif result.get("output"):
                sections.append(f"DNS {record_type} for {domain}:\n{result['output']}")
        search_query = f'"{subject}"'
    elif subject_type == "username":
        if not re.fullmatch(r"[A-Za-z0-9._-]{2,30}", subject):
            return {"ok": False, "error": "Username must be 2-30 characters using letters, digits, dot, underscore, or hyphen."}
        result = _run_public_tool("sherlock", ["--print-found", "--no-color", "--timeout", "8", subject])
        if result.get("missing"):
            missing.add("sherlock")
        elif result.get("output"):
            sections.append("Public username matches (verify each result; names can belong to different people):\n" + result["output"])
        maigret_result = _run_public_tool(
            "maigret",
            ["--no-autoupdate", "--no-recursion", "--no-extracting", "--timeout", "8", "--retries", "0", "-n", "20", "--no-color", "--no-progressbar", subject],
            timeout=120,
        )
        if maigret_result.get("missing"):
            missing.add("maigret")
        elif maigret_result.get("output"):
            sections.append("Additional public username matches (unverified):\n" + maigret_result["output"])
        search_query = f'"{subject}" profile'
    else:
        domain = _valid_domain(subject)
        if not domain:
            return {"ok": False, "error": "Provide a public DNS domain, not an IP or local hostname."}
        result = _run_public_tool("amass", ["enum", "-passive", "-d", domain, "-timeout", "2", "-silent"], timeout=150)
        if result.get("missing"):
            missing.add("amass")
        elif result.get("output"):
            sections.append(f"Passive subdomain observations for {domain}:\n{result['output']}")
        harvest = _run_public_tool("theHarvester", ["-d", domain, "-l", "50", "-q", "-b", "duckduckgo"], timeout=90)
        if harvest.get("missing"):
            missing.add("theHarvester")
        elif harvest.get("output"):
            sections.append(f"Public web/domain search for {domain}:\n{harvest['output']}")
        search_query = f'site:{domain} public security contact OR documentation'

    web_result = search_web(search_query, max_results=6)
    if isinstance(web_result, dict):
        for item in (web_result.get("results") or [])[:6]:
            title = str(item.get("title") or "Untitled")[:240]
            snippet = str(item.get("content") or item.get("snippet") or "")[:700]
            url = str(item.get("url") or item.get("href") or "")[:1000]
            if url:
                urls.append(url)
            sections.append(f"Public web result: {title}\n{snippet}\nSource: {url}")
    else:
        sections.append("Public web search returned no structured results.")

    if missing:
        sections.append("Unavailable local tools (not installed automatically): " + ", ".join(sorted(missing)))
    sections.append("Scope: public sources only. No breach dumps, credentials, private records, or precise personal-location data were queried.")
    return {
        "ok": True,
        "subject_type": subject_type,
        "subject": subject,
        "report": "\n\n".join(sections)[:24_000],
        "sources": list(dict.fromkeys(urls))[:12],
        "missing_tools": sorted(missing),
        "environment": osint_environment(),
    }


def extract_uploaded_file_metadata(file_base64: str, filename: str = "upload.bin") -> dict:
    """Extract a small allowlist of metadata from one explicitly supplied file."""
    if not isinstance(file_base64, str) or not file_base64:
        return {"ok": False, "error": "Attach one file with the /metadata command."}
    try:
        payload = file_base64.strip()
        if "," in payload and "base64" in payload[:100].lower():
            payload = payload.split(",", 1)[1]
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=True)
    except Exception:
        return {"ok": False, "error": "Could not decode the attached file."}
    if not raw or len(raw) > 20 * 1024 * 1024:
        return {"ok": False, "error": "File must be between 1 byte and 20 MiB."}

    suffix = os.path.splitext(os.path.basename(filename or "upload.bin"))[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".bin"
    descriptor, path = tempfile.mkstemp(prefix="creator_metadata_", suffix=suffix)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
        executable = shutil.which("exiftool")
        if executable:
            result = subprocess.run(
                [executable, "-json", "-s", *[f"-{field}" for field in _METADATA_FIELDS], path],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                records = json.loads(result.stdout or "[]")
                metadata = records[0] if records and isinstance(records[0], dict) else {}
                cleaned = {key: str(metadata[key])[:400] for key in _METADATA_FIELDS if key in metadata}
                return {"ok": True, "filename": os.path.basename(filename)[:180], "metadata": cleaned}

        try:
            from PIL import Image
            with Image.open(path) as image:
                tags = image.getexif()
                names = {"Make", "Model", "Software", "DateTime", "DateTimeOriginal", "Orientation", "ImageWidth", "ImageLength"}
                metadata = {}
                for tag_id, value in tags.items():
                    name = Image.ExifTags.TAGS.get(tag_id, str(tag_id))
                    if name in names:
                        metadata[name] = str(value)[:400]
                metadata.update({"FileType": image.format or "unknown", "ImageWidth": image.width, "ImageHeight": image.height})
                return {"ok": True, "filename": os.path.basename(filename)[:180], "metadata": metadata}
        except Exception:
            pass
        return {"ok": False, "filename": os.path.basename(filename)[:180], "error": "No supported metadata was found or exiftool is unavailable."}
    except Exception as exc:
        return {"ok": False, "error": f"Metadata extraction failed: {type(exc).__name__}"}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass