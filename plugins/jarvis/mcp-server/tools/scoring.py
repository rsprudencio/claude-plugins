"""Importance scoring for vault documents.

Computes a 0.0-1.0 importance score from content signals:
- vault_type weight (journal=0.65, note=0.55, work=0.60, etc.)
- concept bonus from regex patterns (decision, incident, TODO)
- recency bonus (exponential decay with configurable half-life)
- retrieval bonus (log-scaled from access count)

Frontmatter 'importance' overrides the type weight component.
"""
import math
import re
from datetime import datetime, timezone
from typing import Optional


# Default type weights (overridable via config)
DEFAULT_TYPE_WEIGHTS = {
    "journal": 0.65,
    "note": 0.55,
    "work": 0.60,
    "inbox": 0.3,
    "incident-log": 0.7,
    "decision": 0.8,
    "unknown": 0.5,
}

# Default concept patterns: regex -> bonus
DEFAULT_CONCEPT_PATTERNS = {
    r"\b(decision|architecture|design)\b": 0.1,
    r"\b(incident|outage|postmortem)\b": 0.15,
    r"\b(TODO|FIXME|HACK)\b": 0.05,
}

_CONCEPT_BONUS_CAP = 0.2
_RECENCY_BONUS_MAX = 0.1
_RETRIEVAL_BONUS_MAX = 0.1


def compute_importance(
    content: str,
    vault_type: str = "unknown",
    frontmatter_importance: Optional[str] = None,
    created_at: Optional[str] = None,
    retrieval_count: int = 0,
    config: Optional[dict] = None,
) -> float:
    """Compute importance score for a document.

    Formula: clamp(type_weight + concept_bonus + recency_bonus + retrieval_bonus, 0.0, 1.0)
    Frontmatter importance replaces type_weight when present.

    Args:
        content: Document text (used for concept pattern matching)
        vault_type: Document type (journal, note, work, etc.)
        frontmatter_importance: Raw importance value from YAML frontmatter
        created_at: ISO timestamp for recency calculation
        retrieval_count: Number of times this doc has been retrieved
        config: Optional config overrides (type_weights, concept_patterns, recency_half_life_days)

    Returns:
        Float between 0.0 and 1.0
    """
    config = config or {}

    # Frontmatter override replaces type weight
    fm_parsed = _parse_frontmatter_importance(frontmatter_importance)
    if fm_parsed is not None:
        base = fm_parsed
    else:
        base = _compute_type_weight(vault_type, config.get("type_weights", {}))

    concept = _compute_concept_bonus(content, config.get("concept_patterns", {}))
    recency = _compute_recency_bonus(created_at, config.get("recency_half_life_days", 7.0))
    retrieval = _compute_retrieval_bonus(retrieval_count)

    return max(0.0, min(1.0, base + concept + recency + retrieval))


def _compute_type_weight(vault_type: str, type_weights: dict) -> float:
    """Look up base weight for a vault type."""
    merged = {**DEFAULT_TYPE_WEIGHTS, **type_weights}
    return merged.get(vault_type, merged.get("unknown", 0.5))


def _compute_concept_bonus(content: str, concept_patterns: dict) -> float:
    """Sum bonuses for concept pattern matches, capped at _CONCEPT_BONUS_CAP."""
    merged = {**DEFAULT_CONCEPT_PATTERNS, **concept_patterns}
    total = 0.0
    for pattern, bonus in merged.items():
        if re.search(pattern, content, re.IGNORECASE):
            total += bonus
    return min(total, _CONCEPT_BONUS_CAP)


def _compute_recency_bonus(created_at: Optional[str], half_life_days: float = 7.0) -> float:
    """Exponential decay bonus based on document age.

    Returns up to _RECENCY_BONUS_MAX for very recent documents,
    decaying with configurable half-life.
    """
    if not created_at:
        return 0.0
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days_old = max(0.0, (now - created).total_seconds() / 86400)
        if half_life_days <= 0:
            return 0.0
        decay = math.exp(-0.693 * days_old / half_life_days)  # ln(2) ≈ 0.693
        return round(decay * _RECENCY_BONUS_MAX, 4)
    except (ValueError, TypeError):
        return 0.0


def _compute_retrieval_bonus(retrieval_count: int) -> float:
    """Log-scaled bonus from retrieval count.

    log2(count + 1) / 10, capped at _RETRIEVAL_BONUS_MAX.
    """
    if retrieval_count <= 0:
        return 0.0
    return min(_RETRIEVAL_BONUS_MAX, round(math.log2(retrieval_count + 1) / 10, 4))


def _parse_frontmatter_importance(value: Optional[str]) -> Optional[float]:
    """Parse frontmatter importance into a float weight.

    Accepts:
      - Numeric strings: "0.8" -> 0.8
      - Named levels: "high" -> 0.8, "critical" -> 0.95, "medium" -> 0.5, "low" -> 0.3
      - None or unrecognized -> None (use type weight instead)
    """
    if value is None:
        return None
    # Try numeric
    try:
        numeric = float(value)
        return max(0.0, min(1.0, numeric))
    except (ValueError, TypeError):
        pass
    # Named levels
    named = {"critical": 0.95, "high": 0.8, "medium": 0.5, "low": 0.3}
    return named.get(str(value).lower().strip())
