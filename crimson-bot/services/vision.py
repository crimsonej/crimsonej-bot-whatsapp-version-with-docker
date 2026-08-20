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


def _convert_webp_to_gif(webp_base64: str) -> str | None:
    """Convert WebP animated sticker to GIF for video model compatibility."""
    try:
        import io
        from PIL import Image
        
        cleaned = _clean_base64_payload(webp_base64)
        raw = base64.b64decode(cleaned)
        
        with Image.open(io.BytesIO(raw)) as img:
            # Check if animated
            if not getattr(img, "is_animated", False) and img.n_frames <= 1:
                log.debug("[Vision] WebP is not animated, sending as-is")
                return None
            
            frames = []
            durations = []
            try:
                for frame_idx in range(img.n_frames):
                    img.seek(frame_idx)
                    # Convert each frame to RGBA for GIF
                    frame = img.convert("RGBA")
                    frames.append(frame)
                    durations.append(img.info.get('duration', 100))
            except EOFError:
                pass
            
            if not frames:
                return None
            
            # Save as GIF
            out = io.BytesIO()
            frames[0].save(
                out, 
                format='GIF', 
                save_all=True, 
                append_images=frames[1:], 
                loop=0, 
                duration=durations[0] if durations else 100,
                disposal=2  # Restore to background
            )
            gif_base64 = base64.b64encode(out.getvalue()).decode('utf-8')
            log.info(f"[Vision] Converted WebP ({img.n_frames} frames) to GIF")
            return gif_base64
    except Exception as exc:
        log.warning(f"[Vision] WebP to GIF conversion failed: {exc}")
        return None

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

def _enhance_image_prompt(prompt: str) -> str:
    """Enhance prompt for better image generation quality."""
    quality_modifiers = [
        "highly detailed",
        "8k resolution",
        "masterpiece",
        "sharp focus",
        "professional photography",
        "perfect lighting",
        "ultra realistic",
        "8k uhd",
        "hdr",
        "vivid colors"
    ]
    # Add quality modifiers if not already present
    prompt_lower = prompt.lower()
    missing = [m for m in quality_modifiers if m.lower() not in prompt_lower]
    if missing:
        prompt = f"{prompt}, {', '.join(missing[:5])}"
    return prompt


def generate_image_nvidia_flux2(prompt: str, width: int = 1024, height: int = 1024, steps: int = 4) -> str | None:
    """Generate image via NVIDIA Flux.2 Klein 4B API with enhanced quality settings."""
    api_key = get_nvidia_key()
    if not api_key:
        return None

    # Enhance prompt for better quality
    enhanced_prompt = _enhance_image_prompt(prompt)
    
    url = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.2-klein-4b"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    # API constraints: cfg_scale <= 1.0, steps within valid range
    payload = {
        "prompt": enhanced_prompt,
        "width": width,
        "height": height,
        "seed": 0,
        "steps": min(max(steps, 1), 20),  # Clamp steps to valid range 1-20
        "cfg_scale": min(max(1.0, 1.0), 1.0),  # Must be <= 1.0
        "sampler": "euler_ancestral"
    }

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
        response = session.post(url, headers=headers, json=payload, timeout=(5, 120))
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
            filename = os.path.join(tempfile.gettempdir(), f"nv_flux2_{timestamp}_{random.randint(1000,9999)}.png")
            with open(filename, "wb") as f:
                f.write(img_data)
            log.info("[Image] NVIDIA Flux2 image saved to %s", filename)
            return filename

    except Exception as e:
        log.error("[Image] NVIDIA Flux2 exception: %s", e)

    return None

def generate_image_auto(prompt: str, max_retries: int = 2, width: int = 1024, height: int = 1024) -> str | None:
    """Primary image generator: tries NVIDIA Flux 2 first, falls back to HF."""
    prompt = prompt[:500]
    img_path = generate_image_nvidia_flux2(prompt, width=width, height=height)
    if img_path:
        return img_path

    api_key = get_hf_key()
    if api_key:
        API_URL = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"inputs": prompt, "parameters": {"width": width, "height": height}, "options": {"wait_for_model": True}}

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
    """Generate high-quality 512x512 WebP sticker with transparency support."""
    # Generate with higher resolution first for better detail
    img_path = generate_image_auto(prompt, width=1024, height=1024)
    if not img_path:
        return None

    webp_path = f"{img_path}.webp"
    cmd = [
        'ffmpeg', '-y', '-i', img_path,
        '-vcodec', 'libwebp',
        '-filter:v', 'scale=512:512:force_original_aspect_ratio=decrease,pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000',
        '-quality', '85',  # Higher quality (was 70)
        '-lossless', '0',  # Near-lossless for better quality
        '-compression_level', '4',  # Better compression efficiency
        '-preset', 'picture',  # Optimize for images
        '-an', webp_path
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


def _clean_base64_payload(data: str) -> str:
    """Clean base64 payload by removing data URL prefix and fixing padding."""
    if not data:
        return ""
    data = data.strip()
    if "," in data:
        data = data.split(",", 1)[1]
    data = data.strip()
    data += "=" * (-len(data) % 4)
    return data


def analyze_video_with_nvidia(
    video_base64: str = "",
    video_url: str = "",
    prompt: str = "Describe this video in detail. What happens, who is in it, what is being said?",
    max_duration_seconds: int = 60,
    max_retries: int = 3
) -> str:
    """
    Analyze video using NVIDIA's Nemotron 3 Nano Omni model.
    Supports video understanding, scene description, speech transcription, and Q&A.
    """
    api_key = get_nvidia_key()
    if not api_key:
        return "NVIDIA API key not configured."

    if not video_base64 and not video_url:
        return "video_base64 or video_url required."

    # Clean base64 if provided
    if video_base64:
        video_base64 = _clean_base64_payload(video_base64)

    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Build content for omni model
    content = [{"type": "text", "text": prompt}]
    
    if video_base64:
        # Detect WebP format and convert to GIF for model compatibility
        if video_base64.startswith("UklGR") or video_base64.startswith("UklGRi"):  # WebP magic bytes
            log.info("[Vision] Detected WebP format, converting to GIF for video model")
            gif_base64 = _convert_webp_to_gif(video_base64)
            if gif_base64:
                video_base64 = gif_base64
                mime_type = "video/gif"
            else:
                mime_type = "video/mp4"  # fallback
        else:
            mime_type = "video/mp4"
        
        content.append({
            "type": "video_url",
            "video_url": {"url": f"data:{mime_type};base64,{video_base64}"}
        })
    elif video_url:
        content.append({
            "type": "video_url",
            "video_url": {"url": video_url}
        })

    payload = {
        "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        "messages": [
            {
                "role": "user",
                "content": content
            }
        ],
        "max_tokens": 1024,
        "temperature": 0.3
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            if response.status_code == 200:
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return content.strip() if content else "No description returned."
            else:
                log.warning(f"NVIDIA Video Analysis attempt {attempt+1} failed: {response.status_code} - {response.text[:200]}")
        except Exception as e:
            log.warning(f"NVIDIA Video Analysis attempt {attempt+1} exception: {e}")
        if attempt < max_retries - 1:
            time.sleep(2)

    return "Could not analyze video."


def parse_document_with_nvidia(
    document_base64: str = "",
    document_url: str = "",
    filename: str = "",
    prompt: str = "Extract all text, tables, and key information from this document.",
    extract_images: bool = False,
    extract_tables: bool = True,
    max_retries: int = 3
) -> dict:
    """
    Parse document using NVIDIA's Vision model (llama-3.2-11b-vision-instruct).
    Extracts text, tables, and key information from PDF, DOCX, PPTX, XLSX, images, etc.
    Converts documents to images and uses vision model for analysis.
    """
    api_key = get_nvidia_key()
    if not api_key:
        return {"ok": False, "error": "NVIDIA API key not configured."}

    if not document_base64 and not document_url:
        return {"ok": False, "error": "document_base64 or document_url required."}

    # Clean base64 if provided
    if document_base64:
        document_base64 = _clean_base64_payload(document_base64)

    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # For PDFs and multi-page documents, we need to render pages as images
    # For now, we'll use the vision model directly on the document
    # The model can handle images directly; PDFs need to be rendered as images first
    
    # Determine MIME type from filename
    mime_type = "application/pdf"
    if filename:
        filename_lower = filename.lower()
        if filename_lower.endswith(".pdf"):
            mime_type = "application/pdf"
        elif filename_lower.endswith(".docx"):
            mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif filename_lower.endswith(".pptx"):
            mime_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        elif filename_lower.endswith(".xlsx"):
            mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif filename_lower.endswith((".jpg", ".jpeg")):
            mime_type = "image/jpeg"
        elif filename_lower.endswith(".png"):
            mime_type = "image/png"
        elif filename_lower.endswith(".webp"):
            mime_type = "image/webp"
        elif filename_lower.endswith(".txt"):
            mime_type = "text/plain"

    # Clean base64 if provided
    if document_base64:
        document_base64 = _clean_base64_payload(document_base64)

    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # For now, send the document directly to the vision model
    # The model can handle PDFs and images directly
    content = [{"type": "text", "text": prompt}]
    
    if document_base64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{document_base64}"}
        })
    elif document_url:
        content.append({
            "type": "image_url",
            "image_url": {"url": document_url}
        })

    payload = {
        "model": "meta/llama-3.2-11b-vision-instruct",
        "messages": [
            {
                "role": "user",
                "content": content
            }
        ],
        "max_tokens": 4096,
        "temperature": 0.1
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            if response.status_code == 200:
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                return {
                    "ok": True,
                    "text": content,
                    "tables": [],  # Vision model doesn't return structured tables
                    "images": [],  # Not extracted separately
                    "metadata": {"model": "meta/llama-3.2-11b-vision-instruct"},
                    "raw_response": content
                }
            else:
                log.warning(f"NVIDIA Document Parse attempt {attempt+1} failed: {response.status_code} - {response.text[:200]}")
        except Exception as e:
            log.warning(f"NVIDIA Document Parse attempt {attempt+1} exception: {e}")
        if attempt < max_retries - 1:
            time.sleep(2)

    return {"ok": False, "error": "Could not parse document."}
