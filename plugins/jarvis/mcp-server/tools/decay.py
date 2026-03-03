"""Time-based importance decay with retrieval reinforcement.

Computes effective importance that decreases over time unless reinforced
by retrieval activity. Key design principle: retrieval reinforcement itself
decays — a memory retrieved 30 days ago gets less boost than one retrieved
yesterday, regardless of total retrieval count.

All computation is pure math (no DB access), intended for query-time use
on the re-ranking candidate set.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone


def compute_effective_importance(
    base_importance: float,
    created_at: datetime,
    last_retrieved_at: datetime | None = None,
    retrieval_count: int = 0,
    *,
    decay_rate: float = 0.05,
    retrieval_half_life_days: int = 30,
    retrieval_boost_max: float = 0.15,
    min_importance: float = 0.05,
    now: datetime | None = None,
) -> float:
    """Compute time-decayed importance with time-decayed retrieval reinforcement.

    Formula:
        age_months = (now - created_at).days / 30
        base_decay = base_importance * (1 - decay_rate) ^ age_months

        if last_retrieved_at:
            days_since = (now - last_retrieved_at).days
            retrieval_boost = boost_max * 0.5 ^ (days_since / half_life)
        else:
            retrieval_boost = 0.0

        effective = clamp(base_decay + retrieval_boost, min_importance, 1.0)

    Args:
        base_importance: Original importance score (0.0-1.0).
        created_at: When the memory was created (timezone-aware).
        last_retrieved_at: When last accessed (None = never retrieved).
        retrieval_count: Total retrieval count (unused in decay formula but
            available for future heuristics).
        decay_rate: Monthly decay rate (default 5% per month).
        retrieval_half_life_days: Retrieval boost halves every N days.
        retrieval_boost_max: Maximum boost from a just-retrieved memory.
        min_importance: Floor — effective importance never drops below this.
        now: Override current time (for testing).

    Returns:
        Effective importance score clamped to [min_importance, 1.0].
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Ensure timezone awareness
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    # Age-based decay: exponential decrease per month
    age_days = max(0, (now - created_at).total_seconds() / 86400)
    age_months = age_days / 30.0
    base_decay = base_importance * ((1.0 - decay_rate) ** age_months)

    # Time-decayed retrieval reinforcement
    retrieval_boost = 0.0
    if last_retrieved_at is not None:
        if last_retrieved_at.tzinfo is None:
            last_retrieved_at = last_retrieved_at.replace(tzinfo=timezone.utc)
        days_since = max(0, (now - last_retrieved_at).total_seconds() / 86400)
        if retrieval_half_life_days > 0:
            retrieval_boost = retrieval_boost_max * (
                0.5 ** (days_since / retrieval_half_life_days)
            )

    # Clamp to [floor, 1.0]
    effective = base_decay + retrieval_boost
    return max(min_importance, min(effective, 1.0))
