"""Tier 2 conflict detection: automatically supersede stale entries.

When new Tier 2 content is written, this module checks for older entries
that the new one contradicts.  Detection uses a hybrid approach:

1. **Negation pre-filter** -- cheap regex gate; most writes exit here.
2. **Embedding similarity + word Jaccard divergence** -- high embedding
   similarity (same topic) combined with low word overlap (different
   wording) signals a likely contradiction.
3. **(Optional) LLM verification** -- when ``use_llm`` is enabled, Haiku
   confirms which candidates are genuinely contradicted.

Superseded entries get ``status: "superseded"`` and ``superseded_by``
metadata; they are then filtered out of query paths (per-prompt
injection, pattern detection) automatically.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import get_conflict_detection_config
from .patterns import STOP_WORDS

logger = logging.getLogger("jarvis-core")

# -- Negation pre-filter ---------------------------------------------------

NEGATION_PATTERN = re.compile(
    r"\b(?:actually|instead|no longer|(?:do|does|did)n['\u2019]t|shouldn['\u2019]t|"
    r"wrong|incorrect|stop using|avoid|deprecated|replaced by|contrary|opposite|"
    r"rather than|never|mistaken|bad practice|anti-pattern|"
    r"not (?:a good|recommended|the right))\b",
    re.IGNORECASE,
)


def has_negation_signals(text: str) -> bool:
    """Return True if *text* contains contradiction / negation markers."""
    if not text:
        return False
    return bool(NEGATION_PATTERN.search(text))


# -- Tokenization ----------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{1,}")


def _tokenize(text: str) -> set[str]:
    """Lowercase, split on non-alpha, filter stop words."""
    if not text:
        return set()
    tokens = set(_TOKEN_RE.findall(text.lower()))
    tokens -= STOP_WORDS
    return tokens


# -- Candidate finding -----------------------------------------------------


def find_conflict_candidates(
    doc_id: str,
    content: str,
    config: dict,
) -> list[dict]:
    """Query PostgreSQL for Tier 2 entries that may conflict with *content*.

    Uses pgvector similarity search to find entries on the same topic,
    then applies word Jaccard divergence to detect contradictions
    (high similarity + low word overlap = likely contradiction).
    """
    from .embedding import get_embedding_service
    from .schema import _get_pool, jsonb_to_metadata

    try:
        service = get_embedding_service()
        query_embedding = service.encode(content)
    except Exception:
        return []

    try:
        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, document, metadata,
                              embedding <=> %s::halfvec AS distance
                       FROM jarvis
                       WHERE metadata->>'tier' = 'chromadb'
                       ORDER BY distance ASC
                       LIMIT %s""",
                    (query_embedding, config["max_candidates"]),
                )
                columns = [desc.name for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception:
        return []

    new_words = _tokenize(content)
    candidates = []

    for row in rows:
        cid = row["id"]
        if cid == doc_id:
            continue
        metadata = jsonb_to_metadata(row["metadata"])
        if metadata.get("status") == "superseded":
            continue

        distance = float(row["distance"])
        similarity = 1 - distance
        if similarity < config["similarity_threshold"]:
            continue

        doc_content = row["document"] or ""
        old_words = _tokenize(doc_content)
        union = new_words | old_words
        jaccard = len(new_words & old_words) / max(len(union), 1)

        if jaccard < config["divergence_threshold"]:
            candidates.append(
                {
                    "id": cid,
                    "content": doc_content,
                    "similarity": similarity,
                    "jaccard": jaccard,
                }
            )

    return candidates


# -- LLM verification (optional) -------------------------------------------

CONFLICT_VERIFICATION_PROMPT = """\
You are a memory conflict detector. Given a NEW entry and EXISTING entries, \
determine which existing ones are contradicted by the new one.

NEW entry:
{new_content}

EXISTING entries:
{candidates_formatted}

Return a JSON object with a "contradicted" key containing an array of indices \
that are CONTRADICTED by the new entry.
Only include entries where the new one directly contradicts or invalidates the old one.
Do NOT include entries that are merely different or complementary.
Return {{"contradicted": []}} if none are contradicted.
"""


def _call_haiku_raw(prompt: str, max_tokens: int = 200) -> Optional[str]:
    """Call Haiku and return raw response text (API first, CLI fallback).

    Lightweight wrapper -- avoids importing the hooks-handlers extraction
    pipeline which parses responses into observation dicts.
    """
    # Try Anthropic SDK first
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except Exception as exc:
            logger.debug(f"Conflict LLM API call failed: {exc}")

    # Fallback: Claude CLI
    try:
        import shutil
        import subprocess

        claude_bin = shutil.which("claude")
        if not claude_bin:
            return None

        env = os.environ.copy()
        env["JARVIS_EXTRACTING"] = "1"
        result = subprocess.run(
            [claude_bin, "-p", "--model", "haiku", "--no-session-persistence"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception as exc:
        logger.debug(f"Conflict LLM CLI call failed: {exc}")

    return None


def verify_conflicts_with_llm(
    new_content: str,
    candidates: list[dict],
    config: dict,
) -> list[str]:
    """Ask Haiku which candidates are truly contradicted.

    Returns list of confirmed conflicting IDs. Falls back to trusting
    rule-based results if LLM call fails.
    """
    formatted = "\n".join(f"[{i}] {c['content']}" for i, c in enumerate(candidates))
    prompt = CONFLICT_VERIFICATION_PROMPT.format(
        new_content=new_content,
        candidates_formatted=formatted,
    )

    response = _call_haiku_raw(prompt)
    if not response:
        # Fallback: trust rule-based
        return [c["id"] for c in candidates]

    try:
        data = json.loads(response)
        indices = data.get("contradicted", [])
        return [
            candidates[i]["id"]
            for i in indices
            if isinstance(i, int) and 0 <= i < len(candidates)
        ]
    except (json.JSONDecodeError, IndexError, TypeError):
        return [c["id"] for c in candidates]


# -- Mark superseded -------------------------------------------------------


def mark_superseded(old_doc_id: str, new_doc_id: str) -> bool:
    """Add ``status: superseded`` and ``superseded_by`` to *old_doc_id*.

    Uses a single SQL UPDATE (no read-then-write round-trip).
    Returns True on success, False on failure (missing doc, database error).
    """
    from .schema import _get_pool

    try:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jarvis
                       SET metadata = metadata
                           || jsonb_build_object(
                               'status', 'superseded'::text,
                               'superseded_by', %s::text,
                               'superseded_at', %s::text),
                           updated_at = now()
                       WHERE id = %s""",
                    (new_doc_id, now_iso, old_doc_id),
                )
                updated = cur.rowcount > 0
                conn.commit()
        return updated
    except Exception as exc:
        logger.debug(f"mark_superseded failed for {old_doc_id}: {exc}")
        return False


# -- Conflict log ----------------------------------------------------------


def _resolve_log_dir() -> Path:
    """Resolve telemetry directory, respecting JARVIS_HOME."""
    env_home = os.environ.get("JARVIS_HOME")
    if env_home:
        return Path(env_home) / "telemetry"
    return Path.home() / ".jarvis" / "telemetry"


def log_conflict(
    old_id: str,
    new_id: str,
    similarity: float,
    jaccard: float,
    method: str,
    verdict: str,
) -> None:
    """Append a JSONL record to ``~/.jarvis/telemetry/conflicts.jsonl``."""
    log_dir = _resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "old_id": old_id,
        "new_id": new_id,
        "similarity": round(similarity, 4),
        "jaccard": round(jaccard, 4),
        "method": method,
        "verdict": verdict,
        "reasoning": (
            f"sim={similarity:.3f} (>= threshold), "
            f"jaccard={jaccard:.3f} (< divergence threshold) "
            f"\u2192 {verdict}"
        ),
    }
    try:
        with open(log_dir / "conflicts.jsonl", "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.debug(f"Failed to write conflict log: {exc}")


# -- Orchestrator ----------------------------------------------------------


def detect_conflicts(doc_id: str, content: str) -> list[str]:
    """Run the full conflict-detection pipeline for a newly written entry.

    Returns list of IDs that were marked ``superseded``.
    """
    config = get_conflict_detection_config()
    if not config.get("enabled", True):
        return []

    # Step 1: Negation pre-filter (cheap gate)
    if not has_negation_signals(content):
        return []

    # Step 2: Find candidates via embedding similarity + Jaccard divergence
    candidates = find_conflict_candidates(doc_id, content, config)
    if not candidates:
        return []

    # Step 3: Optional LLM verification
    use_llm = config.get("use_llm", False)
    if use_llm:
        confirmed_ids = verify_conflicts_with_llm(content, candidates, config)
    else:
        confirmed_ids = [c["id"] for c in candidates]

    method = "llm-verified" if use_llm else "rule-based"

    # Step 4: Mark superseded + log each decision
    superseded = []
    for candidate in candidates:
        old_id = candidate["id"]
        verdict = "superseded" if old_id in confirmed_ids else "retained"
        log_conflict(
            old_id,
            doc_id,
            candidate["similarity"],
            candidate["jaccard"],
            method,
            verdict,
        )
        if verdict == "superseded" and mark_superseded(old_id, doc_id):
            superseded.append(old_id)

    if superseded:
        logger.info(
            f"Conflict detection: {len(superseded)} entries superseded by {doc_id}"
        )

    return superseded
