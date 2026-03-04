"""Internal HTTP endpoint services for hook handlers.

These are intentionally thin wrappers around existing tool internals so the
hook scripts can call stable local HTTP endpoints without importing tools.*
directly.
"""

from __future__ import annotations

import os
from typing import Any

from .config import (
    get_auto_extract_config,
    get_context_enrichment_config,
    get_todoist_prompt_alerts_config,
    get_worklog_config,
)
from .query import query_vault, semantic_context
from .content import content_list, content_write

_DEDUP_JACCARD_THRESHOLD = 0.7
_DEDUP_RELEVANCE_THRESHOLD = 0.95


def _clamp_importance(value: Any, default: float = 0.5) -> float:
    """Parse and clamp importance score to [0.0, 1.0]."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _safe_int(value: Any, default: int) -> int:
    """Best-effort integer parsing with fallback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_tags(value: Any) -> list[str]:
    """Normalize tags to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(tag).strip() for tag in value if str(tag).strip()]


def _safe_str(value: Any) -> str:
    """Convert nullable values to stripped string."""
    if value is None:
        return ""
    return str(value).strip()


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """Compute simple word-overlap Jaccard similarity."""
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a and not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) if union else 0.0


def _is_duplicate_observation(content: str, threshold: float) -> bool:
    """Embedding-relevance dedup for observations."""
    result = query_vault(
        query=content,
        n_results=1,
        filter={"type": "observation"},
    )
    if not result.get("success") or not result.get("results"):
        return False

    top = result["results"][0]
    relevance = float(top.get("relevance", 0.0))
    return relevance >= threshold


def _is_duplicate_worklog(task_summary: str, session_id: str, threshold: float) -> bool:
    """Session-scoped Jaccard dedup for worklogs."""
    result = content_list(
        content_type="worklog",
        session_id=session_id or None,
        sort_by="created_at_desc",
    )
    if not result.get("success") or not result.get("documents"):
        return False

    for doc in result["documents"]:
        existing = _safe_str(doc.get("content"))
        if existing and _jaccard_similarity(task_summary, existing) >= threshold:
            return True
    return False


def _extract_workstreams(limit: int) -> list[str]:
    """Load known workstream names from recent worklog entries."""
    result = content_list(
        content_type="worklog",
        limit=limit,
        sort_by="created_at_desc",
        include_content=False,
    )
    if not result.get("success") or not result.get("documents"):
        return []

    workstreams = set()
    for doc in result["documents"]:
        metadata = doc.get("metadata", {})
        ws = _safe_str(metadata.get("workstream"))
        if ws and ws != "misc":
            workstreams.add(ws)
    return sorted(workstreams)


def _build_common_metadata(context: dict, include_project_dir: bool = False) -> dict:
    """Build shared metadata fields for observation/worklog writes."""
    meta: dict[str, str] = {}

    project_path = _safe_str(context.get("project_path"))
    if project_path:
        meta["project_path"] = project_path
        if include_project_dir:
            meta["project_dir"] = os.path.basename(project_path)

    git_branch = _safe_str(context.get("git_branch"))
    if git_branch:
        meta["git_branch"] = git_branch

    relevant_files = context.get("relevant_files")
    if isinstance(relevant_files, list):
        cleaned = [str(path).strip() for path in relevant_files if str(path).strip()]
        if cleaned:
            meta["relevant_files"] = ",".join(cleaned)

    file_mtimes = context.get("file_mtimes")
    if isinstance(file_mtimes, dict) and file_mtimes:
        meta["file_mtimes"] = file_mtimes

    session_id = _safe_str(context.get("session_id"))
    if session_id:
        meta["session_id"] = session_id

    transcript_line = context.get("transcript_line")
    if transcript_line is not None and transcript_line != "":
        try:
            parsed_line = int(transcript_line)
        except (TypeError, ValueError):
            parsed_line = None
        if parsed_line is not None and parsed_line >= 0:
            meta["transcript_line"] = str(parsed_line)

    return meta


def get_prompt_context(prompt: str) -> dict:
    """Return per-prompt semantic context and todoist alert config."""
    config = get_context_enrichment_config()
    todoist_cfg = get_todoist_prompt_alerts_config()

    budget = _safe_int(config.get("budget"), 8000)
    threshold = float(config.get("threshold", 0.5))
    enabled = bool(config.get("enabled", True))
    debug = bool(config.get("debug", False))

    result = {
        "success": True,
        "enabled": enabled,
        "debug": debug,
        "matches": [],
        "query_ms": 0,
        "total_searched": 0,
        "budget_used": {"core": 0, "vault": 0, "total": budget},
        "todoist_prompt_alerts": {
            "enabled": bool(todoist_cfg.get("enabled", False)),
            "max_per_category": _safe_int(todoist_cfg.get("max_per_category"), 3),
        },
    }

    if not enabled or not _safe_str(prompt):
        return result

    search = semantic_context(
        query=prompt,
        threshold=threshold,
        budget=budget,
        skip_retrieval_increment=False,
    )
    result.update(
        {
            "matches": search.get("matches", []),
            "query_ms": search.get("query_ms", 0),
            "total_searched": search.get("total_searched", 0),
            "budget_used": search.get(
                "budget_used",
                {"core": 0, "vault": 0, "total": budget},
            ),
        }
    )
    return result


def get_auto_extract_context(workstream_limit: int = 30) -> dict:
    """Return extraction/worklog config and known workstreams."""
    auto_extract = get_auto_extract_config()
    worklog = get_worklog_config()
    limit = max(1, min(200, _safe_int(workstream_limit, 30)))

    known_workstreams = []
    if bool(worklog.get("enabled", True)):
        known_workstreams = _extract_workstreams(limit)

    return {
        "success": True,
        "auto_extract": {
            "mode": auto_extract.get("mode", "background"),
            "min_turn_chars": _safe_int(auto_extract.get("min_turn_chars"), 200),
            "max_transcript_lines": _safe_int(
                auto_extract.get("max_transcript_lines"), 500
            ),
            "max_observations": _safe_int(auto_extract.get("max_observations"), 3),
            "dedup_threshold": float(
                auto_extract.get("dedup_threshold", _DEDUP_RELEVANCE_THRESHOLD)
            ),
            "debug": bool(auto_extract.get("debug", False)),
        },
        "worklog": {
            "enabled": bool(worklog.get("enabled", True)),
            "dedup_threshold": float(
                worklog.get("dedup_threshold", _DEDUP_JACCARD_THRESHOLD)
            ),
        },
        "known_workstreams": known_workstreams,
    }


def ingest_auto_extract(payload: dict) -> dict:
    """Persist extracted observations/worklog with dedup checks."""
    observations_payload = payload.get("observations", [])
    if not isinstance(observations_payload, list):
        observations_payload = []

    worklog_payload = payload.get("worklog")
    if worklog_payload is not None and not isinstance(worklog_payload, dict):
        worklog_payload = None

    context = payload.get("context", {})
    if not isinstance(context, dict):
        context = {}

    dedup_cfg = payload.get("dedup", {})
    if not isinstance(dedup_cfg, dict):
        dedup_cfg = {}

    obs_threshold = float(
        dedup_cfg.get("observation_threshold", _DEDUP_RELEVANCE_THRESHOLD)
    )
    worklog_threshold = float(
        dedup_cfg.get("worklog_threshold", _DEDUP_JACCARD_THRESHOLD)
    )

    observation_results = []
    for raw in observations_payload:
        if not isinstance(raw, dict):
            observation_results.append({"status": "error", "id": "", "error": "invalid"})
            continue

        content = _safe_str(raw.get("content"))
        if not content:
            observation_results.append(
                {"status": "error", "id": "", "error": "content is required"}
            )
            continue

        if _is_duplicate_observation(content, obs_threshold):
            observation_results.append({"status": "duplicate", "id": "", "error": ""})
            continue

        metadata = _build_common_metadata(context, include_project_dir=False)
        scope = _safe_str(raw.get("scope"))
        if scope in ("project", "global"):
            metadata["scope"] = scope

        ingest_event_id = _safe_str(raw.get("ingest_event_id"))
        if ingest_event_id:
            metadata["ingest_event_id"] = ingest_event_id

        write_result = content_write(
            content=content,
            content_type="observation",
            importance_score=_clamp_importance(raw.get("importance_score"), default=0.5),
            source="auto-extract:stop-hook",
            tags=_normalize_tags(raw.get("tags")),
            extra_metadata=metadata or None,
            skip_secret_scan=False,
        )

        if write_result.get("success"):
            status = "duplicate" if write_result.get("deduplicated") else "stored"
            observation_results.append(
                {"status": status, "id": write_result.get("id", ""), "error": ""}
            )
        else:
            observation_results.append(
                {
                    "status": "error",
                    "id": "",
                    "error": _safe_str(write_result.get("error")) or "write failed",
                }
            )

    worklog_result = {"status": "duplicate", "id": "", "error": ""}
    if isinstance(worklog_payload, dict):
        task_summary = _safe_str(worklog_payload.get("task_summary"))
        if task_summary:
            session_id = _safe_str(context.get("session_id"))
            if _is_duplicate_worklog(task_summary, session_id, worklog_threshold):
                worklog_result = {"status": "duplicate", "id": "", "error": ""}
            else:
                metadata = _build_common_metadata(context, include_project_dir=True)

                workstream = _safe_str(worklog_payload.get("workstream")) or "misc"
                activity_type = (
                    _safe_str(worklog_payload.get("activity_type")) or "other"
                )
                metadata["workstream"] = workstream
                metadata["activity_type"] = activity_type

                ingest_event_id = _safe_str(worklog_payload.get("ingest_event_id"))
                if ingest_event_id:
                    metadata["ingest_event_id"] = ingest_event_id

                write_result = content_write(
                    content=task_summary,
                    content_type="worklog",
                    importance_score=0.5,
                    source="auto-extract:stop-hook:worklog",
                    tags=_normalize_tags(worklog_payload.get("tags")),
                    extra_metadata=metadata,
                    skip_secret_scan=False,
                )

                if write_result.get("success"):
                    status = (
                        "duplicate" if write_result.get("deduplicated") else "stored"
                    )
                    worklog_result = {
                        "status": status,
                        "id": write_result.get("id", ""),
                        "error": "",
                    }
                else:
                    worklog_result = {
                        "status": "error",
                        "id": "",
                        "error": _safe_str(write_result.get("error")) or "write failed",
                    }
        else:
            worklog_result = {"status": "error", "id": "", "error": "task_summary is required"}

    return {
        "success": True,
        "observations": observation_results,
        "worklog": worklog_result,
    }
