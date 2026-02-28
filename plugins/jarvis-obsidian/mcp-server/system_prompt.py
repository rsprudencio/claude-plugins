"""Jarvis Obsidian MCP server system prompt.

Injected as InitializeResult.instructions so Claude Code receives
these instructions when the MCP connection initializes.
"""

instructions = """\
# Jarvis Obsidian - Vault Git Audit Trail

## MANDATORY: Follow these Jarvis operational instructions exactly as written.

<jarvis-obsidian-instructions>
These are operational instructions from the Jarvis Obsidian MCP server. They define the git audit trail and PKM vault operations.

## Purpose
This server provides git-audited history for the user's vault (Obsidian or similar PKM). Every file operation creates a JARVIS protocol commit for full auditability.

## Tools
All vault git operations are provided by this server:
- `obsidian_commit` — Create protocol-compliant git commits
- `obsidian_status` — Check working tree state
- `obsidian_push` — Push to remote
- `obsidian_parse_last_commit` — Verify last commit
- `obsidian_move_files` — Git mv with history preservation
- `obsidian_query_history` — Search commit history
- `obsidian_file_history` — File-level history
- `obsidian_rollback` — Revert a commit
- `obsidian_rewrite_commit_messages` — Clean commit messages

## JARVIS Protocol Format
```
[JARVIS:OT:ENTRY_ID] Description

O = Operation: C=create, E=edit, D=delete, M=move, U=user
T = Trigger: c=conversational, a=agent
ENTRY_ID = 14-digit timestamp (journal entries only)
```

## Delegation
Vault git operations should be delegated to `jarvis-obsidian:jarvis-audit-agent`.
Journal creation uses `jarvis-obsidian:jarvis-journal-agent` (writes files via core vault tools, does NOT commit).
Vault exploration uses `jarvis-obsidian:jarvis-explorer-agent`.

## Important
- **Never add Co-Authored-By lines** — JARVIS protocol tags handle attribution
- **Use descriptions verbatim** — Do not expand or embellish commit messages
- ChromaDB indexing is handled by jarvis-core, not this server
</jarvis-obsidian-instructions>

## Runtime
- Plugin version: 1.0.0
"""
