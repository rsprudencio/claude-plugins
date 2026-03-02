"""Pattern detection: background scan of observations for recurring themes.

Analyses recent observations for token-set similarity, clusters them into
in-memory candidates, and promotes candidates that exceed a frequency threshold
to durable ``pattern::`` entries in core.memories.

The detection loop runs as a background asyncio task alongside the MCP server.
All database operations are synchronous, so each scan is offloaded to a thread
via ``asyncio.to_thread`` to keep the event loop responsive.
"""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .content import content_write, content_list

logger = logging.getLogger("jarvis-core")

# ── Stop words (filtered from signatures) ────────────────────────────────────

STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "was",
        "were",
        "are",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "need",
        "must",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "it",
        "they",
        "them",
        "this",
        "that",
        "these",
        "those",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "when",
        "where",
        "why",
        "of",
        "in",
        "to",
        "for",
        "with",
        "on",
        "at",
        "from",
        "by",
        "as",
        "into",
        "about",
        "between",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "up",
        "down",
        "out",
        "off",
        "over",
        "under",
        "and",
        "but",
        "or",
        "nor",
        "not",
        "no",
        "so",
        "if",
        "then",
        "than",
        "too",
        "very",
        "just",
        "also",
        "more",
        "most",
        "some",
        "any",
        "all",
        "each",
        "every",
        "both",
        "few",
        "many",
        "much",
        "own",
        "other",
        "such",
        "only",
        "same",
        "here",
        "there",
        "again",
        "once",
    }
)

# ── Pattern type keyword sets ────────────────────────────────────────────────

PATTERN_KEYWORDS: dict[str, set[str]] = {
    "bug": {
        "error",
        "bug",
        "fix",
        "nil",
        "null",
        "crash",
        "exception",
        "failure",
        "broken",
        "issue",
        "wrong",
        "unexpected",
        "traceback",
    },
    "refactor": {
        "refactor",
        "split",
        "extract",
        "rename",
        "consolidate",
        "simplify",
        "cleanup",
        "reorganize",
        "decouple",
        "modularize",
    },
    "architecture": {
        "interface",
        "abstraction",
        "pattern",
        "design",
        "module",
        "layer",
        "boundary",
        "separation",
        "dependency",
        "coupling",
    },
    "anti-pattern": {
        "workaround",
        "hack",
        "todo",
        "fixme",
        "technical-debt",
        "hardcode",
        "magic-number",
        "duplicate",
        "copy-paste",
    },
    "best-practice": {
        "test",
        "lint",
        "validate",
        "document",
        "typing",
        "coverage",
        "logging",
        "monitoring",
        "review",
    },
}

# ── Token extraction regex ───────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{1,}")


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class PatternCandidate:
    """An in-memory cluster of similar observations not yet promoted."""

    key: str  # SHA-256 of sorted signature tokens
    signature: frozenset  # immutable token set (for hashing)
    pattern_type: str
    frequency: int
    first_seen: str  # ISO timestamp
    last_seen: str  # ISO timestamp
    observation_ids: list = field(default_factory=list)
    title: str = ""


# Module-level candidate store — ephemeral, lost on restart
_candidates: dict[str, PatternCandidate] = {}


# ── Pure functions ───────────────────────────────────────────────────────────


def extract_signature(
    content: str, tags: Optional[list] = None, title: Optional[str] = None
) -> set[str]:
    """Extract a token set from observation content, tags, and title.

    Tokenizes to lowercase alphanumeric tokens, strips stop words,
    and unions with tags and title words.
    """
    tokens: set[str] = set()

    # Content tokens
    if content:
        tokens.update(_TOKEN_RE.findall(content.lower()))

    # Tags (already single words typically)
    if tags:
        for tag in tags:
            tokens.update(_TOKEN_RE.findall(tag.lower()))

    # Title words
    if title:
        tokens.update(_TOKEN_RE.findall(title.lower()))

    # Strip stop words
    tokens -= STOP_WORDS

    return tokens


def jaccard_set(a: set, b: set) -> float:
    """Jaccard similarity between two sets. Returns 0.0 for empty inputs."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def classify_pattern_type(signature: set[str], content: str = "") -> str:
    """Classify a pattern type by keyword overlap with the signature.

    Falls back to 'general' if no keyword set dominates.
    """
    best_type = "general"
    best_count = 0

    combined = (
        signature | set(_TOKEN_RE.findall(content.lower())) if content else signature
    )

    for ptype, keywords in PATTERN_KEYWORDS.items():
        count = len(combined & keywords)
        if count > best_count:
            best_count = count
            best_type = ptype

    # Require at least 2 keyword hits to classify as a specific type
    if best_count < 2:
        return "general"
    return best_type


def generate_title(signature: set[str], pattern_type: str) -> str:
    """Generate a human-readable title from top tokens and type."""
    # Pick up to 4 tokens sorted alphabetically for determinism
    top_tokens = sorted(signature)[:4]
    token_str = ", ".join(top_tokens)
    return f"Recurring {pattern_type}: {token_str}"


def compute_confidence(frequency: int, observation_ids: list) -> float:
    """Compute confidence score from frequency and cross-project presence.

    Formula: 0.3 + 0.4 * min(freq, 10) / 10 + project_bonus
    Project bonus: 0.1 if observations span multiple projects, else 0.
    Capped at 1.0.
    """
    freq_component = 0.4 * min(frequency, 10) / 10
    # Cross-project bonus would require metadata inspection; simplified here
    # to a flat 0.3 base + frequency scaling
    confidence = 0.3 + freq_component
    return min(confidence, 1.0)


def _signature_key(signature: frozenset) -> str:
    """Deterministic hash key for a signature."""
    token_str = ",".join(sorted(signature))
    return hashlib.sha256(token_str.encode()).hexdigest()[:16]


# ── Candidate management ────────────────────────────────────────────────────


def create_or_merge_candidate(
    signature: set[str], obs_id: str, obs_content: str, similarity_threshold: float
) -> Optional[PatternCandidate]:
    """Find the best matching candidate or create a new one.

    Returns the candidate that was created or merged into, or None if the
    signature is too small (< 3 tokens).
    """
    if len(signature) < 3:
        return None

    now = datetime.now(timezone.utc).isoformat()
    frozen_sig = frozenset(signature)

    # Try to merge into existing candidate
    best_match: Optional[PatternCandidate] = None
    best_score = 0.0

    for candidate in _candidates.values():
        score = jaccard_set(signature, set(candidate.signature))
        if score >= similarity_threshold and score > best_score:
            best_score = score
            best_match = candidate

    if best_match:
        # Merge: expand signature, bump frequency
        merged_sig = set(best_match.signature) | signature
        best_match.signature = frozenset(merged_sig)
        best_match.frequency += 1
        best_match.last_seen = now
        if obs_id not in best_match.observation_ids:
            best_match.observation_ids.append(obs_id)
        # Update key and re-register
        old_key = best_match.key
        new_key = _signature_key(best_match.signature)
        if old_key != new_key:
            _candidates.pop(old_key, None)
            best_match.key = new_key
            _candidates[new_key] = best_match
        return best_match

    # Create new candidate
    ptype = classify_pattern_type(signature, obs_content)
    title = generate_title(signature, ptype)
    key = _signature_key(frozen_sig)
    candidate = PatternCandidate(
        key=key,
        signature=frozen_sig,
        pattern_type=ptype,
        frequency=1,
        first_seen=now,
        last_seen=now,
        observation_ids=[obs_id],
        title=title,
    )
    _candidates[key] = candidate
    return candidate


def cleanup_candidates(max_candidates: int, expiry_days: int) -> int:
    """Remove expired and excess candidates. Returns count removed."""
    removed = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=expiry_days)

    # Remove expired
    expired_keys = [
        k
        for k, c in _candidates.items()
        if datetime.fromisoformat(c.last_seen) < cutoff
    ]
    for k in expired_keys:
        del _candidates[k]
        removed += 1

    # LRU eviction if still over limit
    if len(_candidates) > max_candidates:
        # Sort by last_seen ascending (oldest first)
        sorted_keys = sorted(_candidates.keys(), key=lambda k: _candidates[k].last_seen)
        to_remove = len(_candidates) - max_candidates
        for k in sorted_keys[:to_remove]:
            del _candidates[k]
            removed += 1

    return removed


# ── Promotion ────────────────────────────────────────────────────────────────


def _find_existing_pattern_match(
    signature: frozenset, merge_threshold: float
) -> Optional[str]:
    """Check if a similar pattern already exists in core.memories.

    Returns the existing pattern's doc ID if Jaccard >= merge_threshold, else None.
    """
    result = content_list(content_type="pattern", limit=100, sort_by="none")
    if not result.get("success") or not result.get("documents"):
        return None

    sig_set = set(signature)
    for doc in result["documents"]:
        meta = doc.get("metadata", {})
        existing_tokens = meta.get("signature_tokens", "")
        if existing_tokens:
            existing_sig = set(existing_tokens.split(","))
            if jaccard_set(sig_set, existing_sig) >= merge_threshold:
                return doc["id"]
    return None


def promote_candidate(candidate: PatternCandidate, merge_threshold: float) -> dict:
    """Promote a candidate to a durable pattern:: entry in core.memories.

    Checks for existing similar patterns first (dedup). If a match is found,
    the existing pattern is not duplicated — we return a note about the merge.

    Returns dict with success status and pattern ID.
    """
    confidence = compute_confidence(candidate.frequency, candidate.observation_ids)
    sig_tokens = ",".join(sorted(candidate.signature))

    # Check for existing similar pattern
    existing_id = _find_existing_pattern_match(candidate.signature, merge_threshold)
    if existing_id:
        logger.info(
            f"Pattern candidate '{candidate.title}' matches existing {existing_id}, skipping"
        )
        return {
            "success": True,
            "action": "merged",
            "existing_id": existing_id,
            "title": candidate.title,
        }

    # Build description
    obs_summary = f"Detected from {len(candidate.observation_ids)} observations"
    description = (
        f"**{candidate.title}**\n\n"
        f"{obs_summary} between {candidate.first_seen} and {candidate.last_seen}.\n\n"
        f"Type: {candidate.pattern_type} | Confidence: {confidence:.2f}\n\n"
        f"Signature tokens: {sig_tokens}"
    )

    # Slugify title for name
    slug = re.sub(r"[^a-z0-9]+", "-", candidate.title.lower()).strip("-")[:60]

    result = content_write(
        content=description,
        content_type="pattern",
        name=slug,
        importance_score=confidence,
        source="pattern-detection",
        tags=["auto-detected"],
        extra_metadata={
            "pattern_type": candidate.pattern_type,
            "observation_count": str(len(candidate.observation_ids)),
            "observation_ids": ",".join(candidate.observation_ids[-20:]),
            "signature_tokens": sig_tokens,
            "first_seen": candidate.first_seen,
        },
    )

    if result.get("success"):
        logger.info(f"Promoted pattern: {candidate.title} (id={result.get('id')})")
    else:
        logger.warning(f"Failed to promote pattern: {result.get('error')}")

    return result


# ── Scan pipeline ────────────────────────────────────────────────────────────


def _fetch_recent_observations(lookback_minutes: int) -> list:
    """Fetch observations created within the lookback window."""
    result = content_list(
        content_type="observation",
        limit=100,
        sort_by="created_at_desc",
    )
    if not result.get("success"):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    recent = []
    for doc in result.get("documents", []):
        meta = doc.get("metadata", {})
        if meta.get("status") == "superseded":
            continue
        created = meta.get("created_at", "")
        if created:
            try:
                doc_time = datetime.fromisoformat(created)
                if doc_time >= cutoff:
                    recent.append(doc)
            except (ValueError, TypeError):
                continue
    return recent


def scan_once(config: dict) -> dict:
    """Run a single pattern detection scan.

    Returns a summary dict with counts of observations processed,
    candidates updated, and patterns promoted.
    """
    observations = _fetch_recent_observations(config["lookback_minutes"])

    candidates_updated = 0
    promoted = 0
    threshold = config["similarity_threshold"]
    promotion_threshold = config["promotion_threshold"]
    merge_threshold = config["merge_threshold"]

    for obs in observations:
        meta = obs.get("metadata", {})
        tags = meta.get("tags", "").split(",") if meta.get("tags") else []
        sig = extract_signature(
            content=obs.get("content", ""),
            tags=tags,
            title=meta.get("title", ""),
        )
        result = create_or_merge_candidate(
            sig, obs["id"], obs.get("content", ""), threshold
        )
        if result:
            candidates_updated += 1

    # Check candidates for promotion
    to_promote = [
        c for c in list(_candidates.values()) if c.frequency >= promotion_threshold
    ]
    for candidate in to_promote:
        result = promote_candidate(candidate, merge_threshold)
        if result.get("success"):
            promoted += 1
            # Remove promoted candidate from in-memory store
            _candidates.pop(candidate.key, None)

    # Cleanup
    cleanup_candidates(config["max_candidates"], config["candidate_expiry_days"])

    return {
        "observations": len(observations),
        "candidates_updated": candidates_updated,
        "promoted": promoted,
        "active_candidates": len(_candidates),
    }


# ── Background loop ─────────────────────────────────────────────────────────

_STARTUP_DELAY = 60  # seconds before first scan


async def pattern_detection_loop():
    """Background loop that periodically scans for patterns.

    Runs alongside the MCP server via asyncio.gather(). Each scan is offloaded
    to a thread since database operations are synchronous.
    """
    from .config import get_pattern_detection_config

    # Wait for server to settle
    await asyncio.sleep(_STARTUP_DELAY)

    while True:
        try:
            config = get_pattern_detection_config()
            if not config["enabled"]:
                logger.debug("Pattern detection disabled, sleeping")
                await asyncio.sleep(config["scan_interval_seconds"])
                continue

            result = await asyncio.to_thread(scan_once, config)
            if result.get("observations", 0) > 0 or result.get("promoted", 0) > 0:
                logger.info(f"Pattern scan: {result}")

        except Exception:
            logger.exception("Error in pattern detection loop")

        config = get_pattern_detection_config()
        await asyncio.sleep(config["scan_interval_seconds"])


def reset_candidates():
    """Clear all in-memory candidates. Used in tests."""
    _candidates.clear()
