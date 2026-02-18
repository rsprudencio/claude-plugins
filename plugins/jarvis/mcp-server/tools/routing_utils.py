"""Shared routing utilities for unified content API (store/retrieve/remove)."""

from typing import Sequence, Tuple


def validate_exactly_one(
    options: Sequence,
    missing_error: str,
    multiple_error: str,
) -> dict | None:
    """Return an error dict if not exactly one of *options* is truthy.

    Returns None when validation passes.
    """
    count = sum(1 for o in options if o)
    if count == 0:
        return {"success": False, "error": missing_error}
    if count > 1:
        return {"success": False, "error": multiple_error}
    return None


def parse_memory_scope(parsed) -> Tuple[str, str | None]:
    """Derive (scope, project) from a ParsedId with a memory:: prefix.

    Returns ("global", None) or ("project", project_name).
    """
    scope = "global" if "global" in parsed.full_prefix else "project"
    project = parsed.project if scope == "project" else None
    return scope, project
