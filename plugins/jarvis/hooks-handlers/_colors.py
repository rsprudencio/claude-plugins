"""Shared ANSI color codes and formatting helpers for debug logging.

Used by extract_observation.py and prompt_search.py to maintain
consistent visual formatting across debug log files.
"""

# ANSI color codes for terminal readability (cat, tail -f, less -R)
C_CYAN = "\033[36m"
C_DIM = "\033[2m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RESET = "\033[0m"

_DIVIDER_WIDTH = 80


def divider_thick() -> str:
    """Cyan ═══ separator to start a new log entry."""
    return f"{C_CYAN}{'═' * _DIVIDER_WIDTH}{C_RESET}"


def divider_section(label: str) -> str:
    """Dim ─── section separator with centered label."""
    pad_total = _DIVIDER_WIDTH - len(label) - 2  # 2 spaces around label
    left = pad_total // 2
    right = pad_total - left
    return f"{C_DIM}{'─' * left} {label} {'─' * right}{C_RESET}"
