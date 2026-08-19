"""
services/rag.py
================
Utilities for chunking documents, managing the `vectors.json` chunk store,
and rebuilding the RAG index with per-chunk metadata (owner, group, source).

This module implements a simple word-based chunker and a reindex function
that writes a list of chunk dicts to `VECTORS_FILE`.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import List

from core.config import DOCS_DIR, VECTORS_FILE, cfg, load_json, save_json, log


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> List[str]:
    size = int(size or cfg("chunk_words") or 400)
    overlap = int(overlap if overlap is not None else cfg("chunk_overlap") or 100)
    words = str(text or "").split()
    if not words:
        return []
    out: List[str] = []
    i = 0
    while i < len(words):
        out.append(" ".join(words[i: i + size]))
        i += max(1, size - overlap)
    return out


def build_index_from_docs(force: bool = False) -> None:
    """Rebuild `VECTORS_FILE` from files in `DOCS_DIR`.

    Each chunk is stored as a dict: {id, text, owner, group, source, ts}
    """
    if not os.path.isdir(DOCS_DIR) or not os.listdir(DOCS_DIR):
        log.info("[RAG] no docs to index in %s", DOCS_DIR)
        save_json(VECTORS_FILE, {"chunks": []})
        return

    chunks: List[dict] = []
    for fname in sorted(os.listdir(DOCS_DIR)):
        fpath = os.path.join(DOCS_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                raw = fh.read()
            for c in chunk_text(raw):
                chunks.append({
                    "id": uuid.uuid4().hex[:12],
                    "text": c,
                    "owner": "",
                    "group": "",
                    "source": fname,
                    "ts": int(time.time()),
                })
        except Exception as e:
            log.warning("[RAG] skipping %s: %s", fname, e)

    save_json(VECTORS_FILE, {"chunks": chunks})
    log.info("[RAG] rebuilt index: %d chunks", len(chunks))


def append_text_to_vectors(text: str, *, owner: str = "", group: str = "", source: str | None = None) -> None:
    """Append chunked text into `VECTORS_FILE` with metadata."""
    if not text:
        return
    existing = load_json(VECTORS_FILE, {"chunks": []}).get("chunks", [])
    max_chunks = int(cfg("rag_max_chunks") or 20000)
    new_chunks = []
    for c in chunk_text(text):
        new_chunks.append({
            "id": uuid.uuid4().hex[:12],
            "text": c,
            "owner": owner or "",
            "group": group or "",
            "source": source or "learned",
            "ts": int(time.time()),
        })
    combined = existing + new_chunks
    # Trim if too large (keep newest)
    if len(combined) > max_chunks:
        combined = combined[-max_chunks:]
    save_json(VECTORS_FILE, {"chunks": combined})
    log.info("[RAG] appended %d chunks (total=%d)", len(new_chunks), len(combined))
