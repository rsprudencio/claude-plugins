#!/usr/bin/env bash
# SessionStart hook: one-time housekeeping per session
# Clean stale watermark files (>30 days old) from previous sessions.
# Runs once at session start, not on every Stop hook fire.
find ~/.jarvis/state/sessions -name "*.json" -mtime +30 -delete 2>/dev/null || true
exit 0
