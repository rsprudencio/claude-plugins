"""Observation staleness tracking via filesystem mtime comparison.

Records file modification times when observations are created, and detects
staleness at query time by comparing recorded mtimes against current values.
Staleness metadata travels in the existing extra_metadata dict on writes,
and is read from already-fetched metadata on queries.
"""

import os

# HFS+ has 1-second granularity; APFS is nanosecond but we keep a small buffer
MTIME_TOLERANCE = 0.01


def record_file_mtimes(file_paths: list[str]) -> dict[str, float]:
    """Record current mtime for each file path.

    Args:
        file_paths: List of absolute or relative file paths.

    Returns:
        Dict mapping file path → mtime (float seconds since epoch).
        Missing/inaccessible files are recorded as 0.0.
    """
    mtimes = {}
    for path in file_paths:
        try:
            mtimes[path] = os.stat(path).st_mtime
        except (OSError, TypeError):
            mtimes[path] = 0.0
    return mtimes


def check_staleness(recorded_mtimes: dict[str, float]) -> dict:
    """Compare recorded mtimes against current filesystem state.

    Args:
        recorded_mtimes: Dict from record_file_mtimes at observation creation time.

    Returns:
        Dict with:
        - is_stale: True if any file has changed or been deleted
        - stale_count: Number of files that changed
        - total_count: Total files tracked
        - stale_files: List of paths that changed
    """
    if not recorded_mtimes:
        return {
            "is_stale": False,
            "stale_count": 0,
            "total_count": 0,
            "stale_files": [],
        }

    stale_files = []
    for path, recorded_mtime in recorded_mtimes.items():
        try:
            current_mtime = os.stat(path).st_mtime
        except (OSError, TypeError):
            # File deleted or inaccessible — stale if it existed at recording time
            if recorded_mtime > 0.0:
                stale_files.append(path)
            continue

        if abs(current_mtime - recorded_mtime) > MTIME_TOLERANCE:
            stale_files.append(path)

    return {
        "is_stale": len(stale_files) > 0,
        "stale_count": len(stale_files),
        "total_count": len(recorded_mtimes),
        "stale_files": stale_files,
    }


def deserialize_mtimes(data) -> dict[str, float]:
    """Extract mtimes from a native dict (pgvector jsonb metadata).

    Returns empty dict on invalid input rather than raising.
    """
    if not isinstance(data, dict):
        return {}
    try:
        return {k: float(v) for k, v in data.items()}
    except (ValueError, TypeError):
        return {}
