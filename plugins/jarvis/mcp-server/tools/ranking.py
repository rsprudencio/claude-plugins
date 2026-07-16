"""Unified retrieval scoring — one formula for every schema (Layer 4).

    score = similarity + importance_weight * (effective_importance - 0.5)

Applied identically to vault chunks, local memories, and remote mirrors:

- No clamp. The old vault formula's min(1.0, ...) pinned saturated chunks in
  an arbitrary tie at exactly 1.0, destroying top-rank ordering.
- Importance is a small additive nudge (±importance_weight/2 at the extremes),
  never a weighted average. The old memory blend (0.7*sim + 0.3*imp) capped
  memories at 0.94 while clamped vault scores reached 1.0 — a perfect-match
  memory could never outrank a saturated vault chunk. No convex weighting can
  fix that; only a single scale can (see passage-ranking-redesign.md, Layer 4).
- effective_importance is decay-adjusted for memories (score_memory) and the
  raw importance_score for vault chunks.
- The relevance threshold gates on raw similarity upstream, never on this
  boosted score — relevance (gate) and ranking (boost) stay decoupled.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .decay import compute_effective_importance

DEFAULT_IMPORTANCE_WEIGHT = 0.24


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


def compute_unified_score(
    similarity: float,
    effective_importance: float,
    *,
    importance_weight: float = DEFAULT_IMPORTANCE_WEIGHT,
) -> float:
    """Score a retrieval candidate on the raw similarity scale.

    Args:
        similarity: Raw cosine similarity (1 - pgvector cosine distance).
        effective_importance: Importance 0-1 (decay-adjusted for memories,
            raw importance_score for vault chunks).
        importance_weight: Scale of the importance nudge (default 0.24,
            i.e. ±0.12 at the importance extremes).

    Returns:
        Unclamped score. Neutral importance (0.5) returns similarity exactly.
    """
    return similarity + importance_weight * (effective_importance - 0.5)


def score_memory(
    similarity: float,
    base_importance: float,
    created_at: datetime,
    last_retrieved_at: Optional[datetime] = None,
    retrieval_count: int = 0,
    *,
    importance_weight: float = DEFAULT_IMPORTANCE_WEIGHT,
    decay_config: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> tuple[float, float]:
    """Score a memory: decay-adjust importance, then apply the unified formula.

    Args:
        similarity: Cosine similarity (0-1, where 1 = identical).
        base_importance: Original importance score (0-1).
        created_at: Memory creation time.
        last_retrieved_at: Last retrieval time (None = never).
        retrieval_count: Total retrieval count.
        importance_weight: Scale of the importance nudge (default 0.24).
        decay_config: Decay parameters (rate, half_life, boost_max, min).
        now: Override current time (for testing).

    Returns:
        Tuple of (score, effective_importance).
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

    score = compute_unified_score(
        similarity, effective_imp, importance_weight=importance_weight
    )
    return score, effective_imp
