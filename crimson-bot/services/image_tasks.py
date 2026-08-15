"""
services/image_tasks.py
========================
Action functions for the dispatcher when an image / sticker task fires.

Mirror the existing `/imagine` and `/sticker` slash-command paths but
return paths instead of pushing to a Flask response, so the dispatcher
can record the result.
"""

from __future__ import annotations

import os
from typing import Any


def _coerce_progress(kwargs: dict) -> Any:
    return kwargs.get("progress") if isinstance(kwargs, dict) else None


def run_generate_image_task(*, prompt: str, owner_jid: str = "",
                             owner_user_id: str = "", progress=None) -> dict:
    """Background image generation. Returns the absolute path to the saved image."""
    import services.vision as vision_svc
    if progress is not None:
        try:
            progress.update(15, "starting")
        except Exception:
            pass
    path = vision_svc.generate_image_auto(prompt)
    if progress is not None:
        try:
            progress.update(85, "saving")
        except Exception:
            pass
    if not path or not os.path.exists(path):
        raise RuntimeError("image generation returned no path")
    if progress is not None:
        try:
            progress.update(100, "done")
        except Exception:
            pass
    return {"path": path, "prompt": prompt, "kind": "image"}


def run_generate_sticker_task(*, prompt: str, owner_jid: str = "",
                               owner_user_id: str = "", progress=None) -> dict:
    """Background sticker generation. The sticker is base64 in memory."""
    import services.vision as vision_svc
    if progress is not None:
        try:
            progress.update(20, "starting")
        except Exception:
            pass
    b64 = vision_svc.generate_sticker_auto(prompt)
    if progress is not None:
        try:
            progress.update(90, "finishing")
        except Exception:
            pass
    if not b64:
        raise RuntimeError("sticker generation returned nothing")
    if progress is not None:
        try:
            progress.update(100, "done")
        except Exception:
            pass
    return {"sticker_b64": b64[:200], "kind": "sticker"}

