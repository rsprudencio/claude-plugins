#!/usr/bin/env python3
"""Harness-neutral turn state for passive auto-extraction.

UserPromptSubmit records the user prompt before retrieval runs. Stop completes
that pending turn with the harness-provided final assistant message. This gives
Codex a stable extraction input without depending on its transcript format,
while Claude's richer transcript parser can remain the preferred source.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_MAX_TURNS_PER_SESSION = 100


def _state_dir() -> Path:
    jarvis_home = os.environ.get("JARVIS_HOME")
    root = Path(jarvis_home).expanduser() if jarvis_home else Path.home() / ".jarvis"
    return root / "state" / "turns"


def _session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _state_path(session_id: str) -> Path:
    return _state_dir() / f"{_session_key(session_id)}.json"


@contextmanager
def _session_lock(session_id: str) -> Iterator[None]:
    directory = _state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"{_session_key(session_id)}.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_state(session_id: str) -> dict:
    try:
        with open(_state_path(session_id), encoding="utf-8") as state_file:
            state = json.load(state_file)
        if isinstance(state, dict) and isinstance(state.get("turns"), list):
            return state
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {"session_id": session_id, "next_sequence": 0, "turns": []}


def _write_state(session_id: str, state: dict) -> None:
    directory = _state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    destination = _state_path(session_id)
    fd, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, separators=(",", ":"))
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _first_string(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _text_content(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _text_content(value.get("text") or value.get("content") or "")
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    return ""


def _session_id(data: dict) -> str:
    return _first_string(data, "session_id", "thread_id", "conversation_id")


def _turn_id(data: dict) -> str:
    return _first_string(data, "turn_id", "message_id")


def _prompt(data: dict) -> str:
    for key in ("prompt", "user_prompt", "message"):
        text = _text_content(data.get(key))
        if text:
            return text
    return ""


def _assistant_message(data: dict) -> str:
    for key in ("last_assistant_message", "assistant_message", "response"):
        text = _text_content(data.get(key))
        if text:
            return text
    return ""


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    seen = set()
    result = []
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _token_usage(data: dict) -> str:
    usage = data.get("usage") or data.get("token_usage")
    if isinstance(usage, str) and usage.strip():
        return usage.strip()
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens", usage.get("input", 0))
        output_tokens = usage.get("output_tokens", usage.get("output", 0))
        try:
            return f"{int(input_tokens)} in, {int(output_tokens)} out"
        except (TypeError, ValueError):
            pass
    return "0 in, 0 out"


def capture_user_prompt(data: dict) -> bool:
    """Persist one UserPromptSubmit payload for later Stop normalization."""
    session_id = _session_id(data)
    prompt = _prompt(data)
    if not session_id or not prompt:
        return False

    turn_id = _turn_id(data)
    with _session_lock(session_id):
        state = _read_state(session_id)
        turns = state["turns"]

        # Hook retries with a stable turn ID update the existing pending turn.
        existing = None
        if turn_id:
            existing = next((turn for turn in turns if turn.get("turn_id") == turn_id), None)
        if existing is not None:
            existing.update(
                {
                    "user_text": prompt,
                    "cwd": _first_string(data, "cwd", "working_directory"),
                    "updated_at": time.time(),
                }
            )
        else:
            sequence = int(state.get("next_sequence", 0))
            turns.append(
                {
                    "sequence": sequence,
                    "turn_id": turn_id,
                    "user_text": prompt,
                    "assistant_text": None,
                    "cwd": _first_string(data, "cwd", "working_directory"),
                    "created_at": time.time(),
                }
            )
            state["next_sequence"] = sequence + 1

        state["turns"] = turns[-_MAX_TURNS_PER_SESSION:]
        _write_state(session_id, state)
    return True


def complete_stop_turn(data: dict, start_sequence: int = 0) -> tuple[list[dict], int, str]:
    """Complete the pending turn and return normalized unprocessed turns.

    Returns ``(turns, last_sequence, first_user_text)``. An empty turn list and
    ``-1`` indicate that the Stop payload could not be paired safely.
    """
    session_id = _session_id(data)
    assistant_text = _assistant_message(data)
    if not session_id or not assistant_text:
        return [], -1, ""

    turn_id = _turn_id(data)
    with _session_lock(session_id):
        state = _read_state(session_id)
        state_turns = state["turns"]
        target = None
        if turn_id:
            target = next(
                (turn for turn in reversed(state_turns) if turn.get("turn_id") == turn_id),
                None,
            )
        if target is None:
            target = next(
                (turn for turn in reversed(state_turns) if not turn.get("assistant_text")),
                None,
            )
        if target is None:
            return [], -1, ""

        target["assistant_text"] = assistant_text
        target["tool_names"] = _string_list(data.get("tool_names") or data.get("tools"))
        target["relevant_files"] = _string_list(
            data.get("relevant_files") or data.get("files")
        )[:10]
        target["token_usage"] = _token_usage(data)
        target["updated_at"] = time.time()
        _write_state(session_id, state)

        complete = [
            turn
            for turn in state_turns
            if int(turn.get("sequence", -1)) >= start_sequence
            and turn.get("user_text")
            and turn.get("assistant_text")
        ]

    normalized = [
        {
            "user_text": turn["user_text"],
            "assistant_text": turn["assistant_text"],
            "tool_names": turn.get("tool_names", []),
            "token_usage": turn.get("token_usage", "0 in, 0 out"),
            "relevant_files": turn.get("relevant_files", []),
            "start_line_idx": int(turn["sequence"]),
            "end_line_idx": int(turn["sequence"]),
        }
        for turn in complete
    ]
    first_user = state_turns[0].get("user_text", "") if state_turns else ""
    last_sequence = normalized[-1]["end_line_idx"] if normalized else -1
    return normalized, last_sequence, first_user


def _read_stdin_json() -> dict:
    try:
        value = json.load(sys.stdin)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError, TypeError):
        return {}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "capture":
        print("Usage: turn_state.py capture", file=sys.stderr)
        return 2
    # Prompt capture must never block a user turn if local state is unavailable.
    try:
        capture_user_prompt(_read_stdin_json())
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
