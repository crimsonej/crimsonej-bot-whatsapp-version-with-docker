"""
services/vector_db.py
=====================
Qdrant vector database integration for semantic search.
Replaces TF-IDF with semantic embeddings for RAG.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from qdrant_client.http.models import Distance, VectorParams

from core.config import cfg, log


# ─────────────────────────────────────────────────────────────────────────────
# QDRANT CLIENT
# ─────────────────────────────────────────────────────────────────────────────

_qdrant_client = None

COLLECTION_NAME = "crimsonej_knowledge"
VECTOR_SIZE = 384  # all-MiniLM-L6-v2 embedding size


def get_qdrant() -> "QdrantClient":
    """Get or create Qdrant client."""
    global _qdrant_client
    if _qdrant_client is None:
        qdrant_url = os.environ.get("QDRANT_URL") or cfg("qdrant_url") or "http://localhost:6333"
        api_key = os.environ.get("QDRANT_API_KEY") or cfg("qdrant_api_key")
        
        _qdrant_client = QdrantClient(
            url=qdrant_url,
            api_key=api_key if api_key else None,
            timeout=30.0,
        )
        
        # Ensure collection exists
        _ensure_collection()
        
        log.info("[VectorDB] Connected to Qdrant at %s", qdrant_url)
    
    return _qdrant_client


def _ensure_collection():
    """Create collection if it doesn't exist."""
    client = _qdrant_client
    try:
        collections = client.get_collections().collections
        names = [c.name for c in collections]
        
        if COLLECTION_NAME not in names:
            client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(
                    size=VECTOR_SIZE,
                    distance=Distance.COSINE,
                ),
            )
            log.info("[VectorDB] Created collection: %s", COLLECTION_NAME)
    except Exception as e:
        log.warning("[VectorDB] Could not ensure collection: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# EMBEDDING MODEL
# ─────────────────────────────────────────────────────────────────────────────

_embedding_model = None


def get_embedding_model():
    """Get or load the embedding model (lazy load)."""
    global _embedding_model
    if _embedding_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            # Use a fast, lightweight model
            _embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
            log.info("[VectorDB] Loaded embedding model: all-MiniLM-L6-v2")
        except Exception as e:
            log.error("[VectorDB] Failed to load embedding model: %s", e)
            raise
    return _embedding_model


def embed_text(text: str) -> List[float]:
    """Generate embedding for text."""
    model = get_embedding_model()
    embedding = model.encode(text, normalize_embeddings=True)
    return embedding.tolist()


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Generate embeddings for multiple texts."""
    model = get_embedding_model()
    embeddings = model.encode(texts, normalize_embeddings=True)
    return embeddings.tolist()


# ─────────────────────────────────────────────────────────────────────────────
# VECTOR OPERATIONS
# ─────────────────────────────────────────────────────────────────────────────

def add_vector(
    text: str,
    metadata: Dict[str, Any],
    vector_id: str | None = None,
) -> str:
    """Add a text chunk with embedding to Qdrant."""
    client = get_qdrant()
    vector_id = vector_id or str(uuid.uuid4())
    
    embedding = embed_text(text)
    
    point = qmodels.PointStruct(
        id=vector_id,
        vector=embedding,
        payload={
            "text": text,
            "created_at": time.time(),
            **metadata,
        },
    )
    
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[point],
    )
    
    return vector_id


def add_vectors_batch(
    texts: List[str],
    metadatas: List[Dict[str, Any]],
    vector_ids: Optional[List[str]] = None,
) -> List[str]:
    """Add multiple vectors in batch."""
    client = get_qdrant()
    
    if vector_ids is None:
        vector_ids = [str(uuid.uuid4()) for _ in texts]
    
    embeddings = embed_texts(texts)
    
    points = []
    for i, (text, metadata, vector_id, embedding) in enumerate(zip(texts, metadatas, vector_ids, embeddings)):
        point = qmodels.PointStruct(
            id=vector_id,
            vector=embedding,
            payload={
                "text": text,
                "created_at": time.time(),
                **metadata,
            },
        )
        points.append(point)
    
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=points,
    )
    
    return vector_ids


def search_vectors(
    query: str,
    limit: int = 5,
    filter_metadata: Optional[Dict[str, Any]] = None,
    score_threshold: float = 0.7,
) -> List[Dict[str, Any]]:
    """Search for similar vectors."""
    client = get_qdrant()
    
    query_vector = embed_text(query)
    
    # Build filter if metadata filter provided
    filter_condition = None
    if filter_metadata:
        conditions = []
        for key, value in filter_metadata.items():
            conditions.append(
                qmodels.FieldCondition(
                    key=key,
                    match=qmodels.MatchValue(value=value),
                )
            )
        if conditions:
            filter_condition = qmodels.Filter(must=conditions)
    
    search_result = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_vector,
        query_filter=filter_condition,
        limit=limit,
        score_threshold=score_threshold,
        with_payload=True,
        with_vectors=False,
    )
    
    results = []
    for hit in search_result:
        results.append({
            "id": hit.id,
            "score": hit.score,
            "text": hit.payload.get("text", ""),
            "metadata": {k: v for k, v in hit.payload.items() if k != "text"},
        })
    
    return results


def delete_vector(vector_id: str) -> bool:
    """Delete a vector by ID."""
    client = get_qdrant()
    try:
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=qmodels.PointIdsList(points=[vector_id]),
        )
        return True
    except Exception:
        return False


def delete_by_metadata(filter_metadata: Dict[str, Any]) -> int:
    """Delete vectors matching metadata filter."""
    client = get_qdrant()
    
    conditions = []
    for key, value in filter_metadata.items():
        conditions.append(
            qmodels.FieldCondition(
                key=key,
                match=qmodels.MatchValue(value=value),
            )
        )
    
    filter_condition = qmodels.Filter(must=conditions) if conditions else None
    
    try:
        result = client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=qmodels.FilterSelector(filter=filter_condition),
        )
        return result.operation_id if hasattr(result, 'operation_id') else 0
    except Exception:
        return 0


def get_collection_info() -> Dict[str, Any]:
    """Get collection statistics."""
    client = get_qdrant()
    try:
        info = client.get_collection(COLLECTION_NAME)
        return {
            "name": info.config.params.vectors.name if hasattr(info.config.params, 'vectors') else COLLECTION_NAME,
            "vectors_count": info.vectors_count,
            "indexed_vectors_count": info.indexed_vectors_count,
            "points_count": info.points_count,
            "status": info.status,
        }
    except Exception as e:
        return {"error": str(e)}


def hybrid_search(
    query: str,
    tfidf_results: List[Dict],
    limit: int = 5,
    alpha: float = 0.5,
) -> List[Dict]:
    """
    Hybrid search combining TF-IDF and vector similarity.
    
    Args:
        query: Search query
        tfidf_results: Results from TF-IDF search (list of dicts with 'text', 'score')
        limit: Number of results to return
        alpha: Weight for vector search (1-alpha for TF-IDF)
    
    Returns:
        Combined and reranked results
    """
    # Get vector results
    vector_results = search_vectors(query, limit=limit * 2)
    
    # Normalize scores
    vector_scores = {r["id"]: r["score"] for r in vector_results}
    tfidf_scores = {r.get("id", f"tfidf_{i}"): r.get("score", 0) for i, r in enumerate(tfidf_results)}
    
    # Combine scores
    all_ids = set(vector_scores.keys()) | set(tfidf_scores.keys())
    combined = []
    
    for id_ in all_ids:
        v_score = vector_scores.get(id_, 0)
        t_score = tfidf_scores.get(id_, 0)
        combined_score = alpha * v_score + (1 - alpha) * t_score
        
        # Find the result object
        result = next((r for r in vector_results if r["id"] == id_), None)
        if not result and tfidf_results:
            result = next((r for r in tfidf_results if r.get("id") == id_), None)
        
        if result:
            combined.append({
                **result,
                "hybrid_score": combined_score,
                "vector_score": v_score,
                "tfidf_score": t_score,
            })
    
    # Sort by combined score
    combined.sort(key=lambda x: x["hybrid_score"], reverse=True)
    return combined[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# MIGRATION FROM TF-IDF
# ─────────────────────────────────────────────────────────────────────────────

def migrate_from_tfidf(tfidf_chunks: List[Dict]) -> int:
    """Migrate existing TF-IDF chunks to Qdrant."""
    texts = []
    metadatas = []
    
    for chunk in tfidf_chunks:
        if isinstance(chunk, str):
            texts.append(chunk)
            metadatas.append({"source": "tfidf_migration"})
        elif isinstance(chunk, dict) and "text" in chunk:
            texts.append(chunk["text"])
            meta = {k: v for k, v in chunk.items() if k != "text"}
            meta["source"] = "tfidf_migration"
            metadatas.append(meta)
    
    if not texts:
        return 0
    
    vector_ids = add_vectors_batch(texts, metadatas)
    log.info("[VectorDB] Migrated %d chunks from TF-IDF", len(vector_ids))
    return len(vector_ids)