#!/usr/bin/env python3
"""PreCompact dedup gate: prevents redundant memory injection within a compaction window.

Usage:
  Hook mode:  echo '{"session_id":"..."}' | python3 precompact_dedup.py --hook
  Cleanup:    python3 precompact_dedup.py --cleanup

Called by the PreCompact hook to clear session injection state, forcing
context_enrichment.py to re-inject all relevant memories after compaction.

Dedup flow:
- Prompt N: context_enrichment injects memories → writes hashes to session state
- Prompt N+1: context_enrichment reads state → skips already-injected hashes
- PreCompact fires: CLEARS session state
- Prompt N+2 (post-compact): state empty → re-injects all relevant memories

State files: ~/.jarvis/state/sessions/<session_id>_injection.json
One per session, accumulates hashes within a compaction window.
Cleared by PreCompact hook. Stale files cleaned by SessionStart (>24h).
"""
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Session-scoped injection state directory
STATE_DIR = Path.home() / ".jarvis" / "state" / "sessions"

# State expires after this many seconds (24 hours)
MARKER_MAX_AGE_SECONDS = 86400


def _injection_state_path(session_id: str) -> Path:
    """Return session-scoped injection state file path."""
    return STATE_DIR / f"{session_id}_injection.json"


def compute_content_hash(content: str) -> str:
    """Compute SHA-256 hash of normalized content for dedup comparison."""
    normalized = " ".join(content.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def read_injection_state(session_id: str) -> dict:
    """Read the current injection state for a session.

    Returns:
        Dict with keys: session_id, timestamp, content_hashes, sources.
        Empty dict if no state exists.
    """
    if not session_id:
        return {}
    path = _injection_state_path(session_id)
    try:
        if path.exists():
            with open(path, "r") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def write_injection_state(
    session_id: str, content_hashes: list[str], sources: list[str]
) -> None:
    """Append newly-injected hashes to the session's injection state.

    Merges with existing state (set union) to accumulate hashes across
    multiple prompts within a single compaction window.

    Args:
        session_id: Current session identifier
        content_hashes: SHA-256 hashes of injected content blocks
        sources: Source identifiers of injected memories
    """
    if not session_id:
        return
    try:
        existing = read_injection_state(session_id)
        merged_hashes = list(set(existing.get("content_hashes", [])) | set(content_hashes))
        merged_sources = list(set(existing.get("sources", [])) | set(sources))
        state = {
            "session_id": session_id,
            "timestamp": time.time(),
            "content_hashes": merged_hashes,
            "sources": merged_sources,
        }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _injection_state_path(session_id)
        # Atomic write
        fd, tmp_path = tempfile.mkstemp(
            dir=str(STATE_DIR), prefix=".injection_state_", suffix=".tmp"
        )
        try:
            os.write(fd, json.dumps(state).encode("utf-8"))
            os.close(fd)
            os.replace(tmp_path, str(path))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        pass  # Never fail on state writes


def filter_already_injected(matches: list[dict], session_id: str) -> list[dict]:
    """Filter out matches already injected in this compaction window.

    Args:
        matches: List of memory match dicts (with "content" and "source" keys)
        session_id: Current session identifier

    Returns:
        Filtered list with already-injected content removed
    """
    if not session_id:
        return matches  # No session context, can't filter

    state = read_injection_state(session_id)
    if not state:
        return matches

    # Age check (24h TTL)
    if time.time() - state.get("timestamp", 0) > MARKER_MAX_AGE_SECONDS:
        return matches

    existing_hashes = set(state.get("content_hashes", []))
    if not existing_hashes:
        return matches

    return [m for m in matches if compute_content_hash(m.get("content", "")) not in existing_hashes]


def clear_injection_state(session_id: str) -> None:
    """Clear injection state so memories get re-injected after compaction."""
    if not session_id:
        return
    path = _injection_state_path(session_id)
    try:
        if path.exists():
            os.unlink(path)
    except OSError:
        pass


def cleanup_stale_states(max_age_seconds: int = MARKER_MAX_AGE_SECONDS) -> None:
    """Remove injection state files older than max_age."""
    try:
        for f in STATE_DIR.glob("*_injection.json"):
            if time.time() - f.stat().st_mtime > max_age_seconds:
                os.unlink(f)
    except OSError:
        pass


def main():
    """Entry point for PreCompact hook and cleanup."""
    if len(sys.argv) >= 2 and sys.argv[1] == "--cleanup":
        cleanup_stale_states()
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] == "--hook":
        session_id = ""
        try:
            data = json.loads(sys.stdin.read())
            session_id = data.get("session_id", "")
        except Exception:
            pass
        if session_id:
            clear_injection_state(session_id)
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
