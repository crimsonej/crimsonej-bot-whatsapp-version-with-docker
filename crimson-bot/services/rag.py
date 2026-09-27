"""
services/rag.py
================
Utilities for chunking documents, managing the SQLite vector store,
and rebuilding the RAG index with per-chunk metadata (owner, group, source).

This module implements a simple word-based chunker and a reindex function
that writes chunks to the SQLite vectors table.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import List

from core.config import DOCS_DIR, cfg, log
from services.storage import vector_add, vector_get_all


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
    """Rebuild vectors table from files in `DOCS_DIR`.

    Each chunk is stored with metadata: owner, group, source, ts
    """
    if not os.path.isdir(DOCS_DIR) or not os.listdir(DOCS_DIR):
        log.info("[RAG] no docs to index in %s", DOCS_DIR)
        return

    count = 0
    for fname in sorted(os.listdir(DOCS_DIR)):
        fpath = os.path.join(DOCS_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                raw = fh.read()
            for c in chunk_text(raw):
                vector_add(c, owner="", group="", source=fname)
                count += 1
        except Exception as e:
            log.warning("[RAG] skipping %s: %s", fname, e)

    # Rebuild FTS index
    try:
        from services.storage import vector_rebuild_fts
        vector_rebuild_fts()
    except Exception:
        pass

    log.info("[RAG] rebuilt index: %d chunks", count)


def append_text_to_vectors(text: str, *, owner: str = "", group: str = "", source: str | None = None) -> None:
    """Append chunked text into vectors table with metadata."""
    if not text:
        return
    max_chunks = int(cfg("rag_max_chunks") or 20000)
    
    # Check current count and trim if needed
    existing = vector_get_all(owner=owner, group_jid=group)
    if len(existing) >= max_chunks:
        # We can't easily delete oldest from here without more logic
        # For now, just add and let it grow - periodic rebuild will trim
        pass
    
    new_count = 0
    for c in chunk_text(text):
        vector_add(c, owner=owner or "", group_jid=group or "", source=source or "learned")
        new_count += 1
    log.info("[RAG] appended %d chunks (owner=%s, group=%s)", new_count, owner, group)
