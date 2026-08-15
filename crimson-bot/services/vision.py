"""
services/vision.py
==================
Vision intelligence (NVIDIA Nemotron Nano VL) and Image/Sticker generation (NVIDIA Flux 2 & HF).
"""

from __future__ import annotations

import base64
import io
import os
import random
import socket
import subprocess
import tempfile
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image

from core.config import get_nvidia_key, get_hf_key, log

def _clean_base64_payload(image_base64: str) -> str:
    image_base64 = (image_base64 or "").strip()
    if "," in image_base64:
        image_base64 = image_base64.split(",", 1)[1]
    return image_base64 + "=" * (-len(image_base64) % 4)

def _normalize_image_for_vlm(image_base64: str) -> tuple[str, str]:
    """Convert WhatsApp media, especially WebP stickers, into a VLM-friendly PNG."""
    cleaned = _clean_base64_payload(image_base64)
    try:
        raw = base64.b64decode(cleaned)
        with Image.open(io.BytesIO(raw)) as img:
            try:
                img.seek(0)
            except EOFError:
                pass
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            if max(img.size) > 1024:
                img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
        normalized = base64.b64encode(out.getvalue()).decode("utf-8")
        return normalized, "image/png"
    except Exception as exc:
        log.warning("[Vision] Could not normalize image payload, sending original: %s", exc)
        return cleaned, "image/jpeg"

def analyze_image_with_nvidia(image_base64: str, prompt: str = "Describe this image in detail.", max_retries: int = 3) -> str:
    """Analyze image using NVIDIA's Llama-3.1-Nemotron-Nano-VL model."""
    api_key = get_nvidia_key()
    if not api_key:
        return "NVIDIA API key not configured."

    image_base64, mime_type = _normalize_image_for_vlm(image_base64)

    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}}
                ]
            }
        ],
        "max_tokens": 512,
        "temperature": 0.3
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            if response.status_code == 200:
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return content.strip() if content else "No description returned."
            else:
                log.warning(f"NVIDIA VLM attempt {attempt+1} failed: {response.status_code} - {response.text[:200]}")
        except Exception as e:
            log.warning(f"NVIDIA VLM attempt {attempt+1} exception: {e}")
        if attempt < max_retries - 1:
            time.sleep(2)

    return "Could not analyze image/sticker."

def generate_image_nvidia_flux2(prompt: str, width: int = 1024, height: int = 1024, steps: int = 4) -> str | None:
    """Generate image via NVIDIA Flux.2 Klein 4B API."""
    api_key = get_nvidia_key()
    if not api_key:
        return None

    url = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.2-klein-4b"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    payload = {"prompt": prompt, "width": width, "height": height, "seed": 0, "steps": steps}

    # Hardened request: pre-resolve DNS, use session with retries for transient network/DNS issues
    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("HEAD", "GET", "POST")
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))

    # Try a few quick DNS resolves before issuing the request to fail fast on name resolution
    host = "ai.api.nvidia.com"
    resolved = False
    for attempt in range(3):
        try:
            socket.getaddrinfo(host, 443)
            resolved = True
            break
        except Exception as e:
            log.warning("[Image] DNS resolve attempt %d failed for %s: %s", attempt + 1, host, e)
            time.sleep(1)

    if not resolved:
        log.error("[Image] DNS resolution failed for %s — skipping Flux2 call", host)
        return None

    try:
        response = session.post(url, headers=headers, json=payload, timeout=(5, 60))
        if response.status_code != 200:
            log.error("[Image] NVIDIA Flux2 error %s: %s", response.status_code, response.text[:200])
            return None

        content_type = response.headers.get("Content-Type", "").lower()
        img_data = None

        if "image/" in content_type:
            img_data = response.content
        else:
            try:
                data = response.json()
                if "artifacts" in data and isinstance(data["artifacts"], list) and data["artifacts"]:
                    art = data["artifacts"][0]
                    if art.get("base64"):
                        img_data = base64.b64decode(art["base64"])
            except Exception as e:
                log.error("[Image] Flux2 JSON decode error: %s", e)

        if img_data:
            timestamp = int(time.time())
            filename = os.path.join(tempfile.gettempdir(), f"nv_flux2_{timestamp}_{random.randint(1000,9999)}.jpg")
            with open(filename, "wb") as f:
                f.write(img_data)
            log.info("[Image] NVIDIA Flux2 image saved to %s", filename)
            return filename

    except Exception as e:
        log.error("[Image] NVIDIA Flux2 exception: %s", e)

    return None

def generate_image_auto(prompt: str, max_retries: int = 2) -> str | None:
    """Primary image generator: tries NVIDIA Flux 2 first, falls back to HF."""
    prompt = prompt[:500]
    img_path = generate_image_nvidia_flux2(prompt)
    if img_path:
        return img_path

    api_key = get_hf_key()
    if api_key:
        API_URL = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"inputs": prompt, "options": {"wait_for_model": True}}

        for attempt in range(max_retries):
            try:
                response = requests.post(API_URL, headers=headers, json=payload, timeout=60)
                if response.status_code == 200:
                    filename = os.path.join(tempfile.gettempdir(), f"hf_{int(time.time())}_{random.randint(1000,9999)}.png")
                    with open(filename, 'wb') as f:
                        f.write(response.content)
                    log.info("[Image] HF Success: Saved to %s", filename)
                    return filename
            except Exception as e:
                log.error("[Image] HF Exception: %s", e)
            if attempt < max_retries - 1:
                time.sleep(2)

    return None


def generate_image_edit_auto(prompt: str, image_base64: str | None = None, max_retries: int = 2) -> str | None:
    # Image-edit feature removed. Keep stub for compatibility.
    log.info("[Image-Edit] feature disabled: generate_image_edit_auto() called but is removed.")
    return None

def generate_sticker_auto(prompt: str) -> str | None:
    """Generate image and convert to a 512x512 WebP sticker base64 string."""
    img_path = generate_image_auto(prompt)
    if not img_path:
        return None

    webp_path = f"{img_path}.webp"
    cmd = [
        'ffmpeg', '-i', img_path,
        '-vcodec', 'libwebp',
        '-filter:v', 'scale=512:512:force_original_aspect_ratio=increase,crop=512:512',
        '-quality', '70',
        '-loop', '0', '-an', webp_path, '-y'
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        with open(webp_path, 'rb') as f:
            b64_data = base64.b64encode(f.read()).decode('utf-8')
        if os.path.exists(img_path): os.remove(img_path)
        if os.path.exists(webp_path): os.remove(webp_path)
        return b64_data
    except Exception as e:
        log.error("[Sticker] Conversion error: %s", e)
        if os.path.exists(img_path): os.remove(img_path)
        return None
