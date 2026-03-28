"""
Cross-encoder reranking for indicator resolution.

Uses FlashRank (4MB model, CPU-only) to rerank candidate indicators
after initial FTS5+FAISS retrieval. Cross-encoders attend to both
the query and each candidate simultaneously, producing much better
relevance scores than independent embedding comparisons.

Architecture: Retrieve (FTS5+FAISS) → Rerank (FlashRank) → Select best
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Lazy-load FlashRank to avoid import cost on every request
_ranker = None
_ranker_failed = False


def _get_ranker():
    """Lazy-initialize the FlashRank ranker (downloads ~4MB model on first use)."""
    global _ranker, _ranker_failed
    if _ranker is not None:
        return _ranker
    if _ranker_failed:
        return None
    try:
        from flashrank import Ranker, RerankRequest  # noqa: F401
        _ranker = Ranker(model_name="ms-marco-TinyBERT-L-2-v2", cache_dir="/tmp/flashrank_cache")
        logger.info("FlashRank reranker initialized (ms-marco-TinyBERT-L-2-v2)")
        return _ranker
    except Exception as e:
        _ranker_failed = True
        logger.warning("FlashRank not available, falling back to score-based ranking: %s", e)
        return None


def rerank_candidates(
    query: str,
    candidates: List[Dict],
    top_k: int = 10,
) -> List[Tuple[Dict, float]]:
    """
    Rerank indicator candidates using a cross-encoder model.

    Args:
        query: The user's natural language query
        candidates: List of indicator dicts with 'name', 'code', 'description' fields
        top_k: Number of top results to return

    Returns:
        List of (candidate_dict, rerank_score) tuples, sorted by relevance.
        If FlashRank is unavailable, returns candidates unchanged with score 0.0.
    """
    if not candidates or not query:
        return [(c, 0.0) for c in candidates[:top_k]]

    ranker = _get_ranker()
    if ranker is None:
        # Fallback: return as-is (original FTS5/FAISS ordering)
        return [(c, 0.0) for c in candidates[:top_k]]

    try:
        from flashrank import RerankRequest

        # Build passage texts from candidate metadata
        passages = []
        for c in candidates:
            name = str(c.get("name") or "")
            code = str(c.get("code") or "")
            desc = str(c.get("description") or "")
            provider = str(c.get("provider") or "")
            # Combine into a rich passage for the cross-encoder
            text = f"{name} ({code})"
            if desc:
                text += f" — {desc[:200]}"
            if provider:
                text += f" [{provider}]"
            passages.append({"id": code or str(len(passages)), "text": text, "meta": c})

        rerank_request = RerankRequest(query=query, passages=passages)
        results = ranker.rerank(rerank_request)

        # Sort by score descending and return top_k
        reranked = []
        for r in sorted(results, key=lambda x: x.get("score", 0), reverse=True)[:top_k]:
            meta = r.get("meta", {})
            score = float(r.get("score", 0))
            reranked.append((meta, score))

        if reranked:
            logger.info(
                "Reranked %d candidates for '%s': top=%s (%.3f)",
                len(candidates),
                query[:40],
                reranked[0][0].get("code", "?"),
                reranked[0][1],
            )

        return reranked

    except Exception as e:
        logger.warning("FlashRank reranking failed, using original order: %s", e)
        return [(c, 0.0) for c in candidates[:top_k]]
