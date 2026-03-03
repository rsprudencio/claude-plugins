"""Two-phase retrieval with blended scoring.

Phase 1 (SQL): pgvector HNSW finds top-N nearest neighbors by embedding distance.
Phase 2 (Python): Re-rank candidates by blending similarity + effective importance.

This replaces the simple _compute_relevance() model with a configurable
weighted blend that incorporates importance decay.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .decay import compute_effective_importance


def _parse_datetime(value) -> Optional[datetime]:
    """Parse a datetime from various formats (str, datetime, None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
    return None


def compute_blended_score(
    similarity: float,
    base_importance: float,
    created_at: datetime,
    last_retrieved_at: Optional[datetime] = None,
    retrieval_count: int = 0,
    *,
    similarity_weight: float = 0.7,
    importance_weight: float = 0.3,
    decay_config: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> tuple[float, float]:
    """Compute blended score from similarity and effective importance.

    Args:
        similarity: Cosine similarity (0-1, where 1 = identical).
        base_importance: Original importance score (0-1).
        created_at: Memory creation time.
        last_retrieved_at: Last retrieval time (None = never).
        retrieval_count: Total retrieval count.
        similarity_weight: Weight for similarity in blend (default 0.7).
        importance_weight: Weight for importance in blend (default 0.3).
        decay_config: Decay parameters (rate, half_life, boost_max, min).
        now: Override current time (for testing).

    Returns:
        Tuple of (blended_score, effective_importance).
    """
    if decay_config is None:
        decay_config = {}

    effective_imp = compute_effective_importance(
        base_importance,
        created_at,
        last_retrieved_at,
        retrieval_count,
        decay_rate=decay_config.get("rate_per_month", 0.05),
        retrieval_half_life_days=decay_config.get("retrieval_half_life_days", 30),
        retrieval_boost_max=decay_config.get("retrieval_boost_max", 0.15),
        min_importance=decay_config.get("min_importance", 0.05),
        now=now,
    )

    blended = (similarity_weight * similarity) + (importance_weight * effective_imp)
    return blended, effective_imp


def rerank_candidates(
    candidates: list[dict],
    *,
    similarity_weight: float = 0.7,
    importance_weight: float = 0.3,
    decay_config: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Re-rank query candidates by blended score.

    Each candidate dict must have:
    - 'similarity': float (cosine similarity 0-1)
    - 'base_importance': float (0-1)
    - 'created_at': datetime or ISO string
    - 'last_retrieved_at': datetime, ISO string, or None (optional)
    - 'retrieval_count': int (optional, default 0)

    Adds to each candidate:
    - 'blended_score': weighted combination
    - 'effective_importance': decay-adjusted importance

    Returns candidates sorted by blended_score descending.
    """
    for c in candidates:
        created = _parse_datetime(c.get("created_at"))
        if created is None:
            created = datetime.now(timezone.utc)

        last_retrieved = _parse_datetime(c.get("last_retrieved_at"))

        blended, eff_imp = compute_blended_score(
            similarity=c.get("similarity", 0.0),
            base_importance=c.get("base_importance", 0.5),
            created_at=created,
            last_retrieved_at=last_retrieved,
            retrieval_count=int(c.get("retrieval_count", 0)),
            similarity_weight=similarity_weight,
            importance_weight=importance_weight,
            decay_config=decay_config,
            now=now,
        )
        c["blended_score"] = blended
        c["effective_importance"] = eff_imp

    candidates.sort(key=lambda c: c["blended_score"], reverse=True)
    return candidates
