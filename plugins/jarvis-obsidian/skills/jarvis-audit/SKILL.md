---
name: jarvis-audit
description: Git audit protocol reference. Background knowledge for JARVIS protocol commits.
user-invocable: false
---

# JARVIS protocol - Git Audit System

Every file operation creates an auditable git commit via `jarvis-audit-agent`.

## Delegation Pattern

**Never run git commands directly in the vault. Always delegate vault commits to `jarvis-audit-agent`.**

Note: This only applies to the vault. Git commands in other repositories are unaffected.

```json
{
  "operation": "create|edit|delete|move|user",
  "description": "what was done",
  "entry_id": "YYYYMMDDHHMMSS",
  "files": ["path/to/file.md"]
}
```

## Agent Handles

- Auto user prologue: `obsidian_commit` automatically commits dirty files as `[JARVIS:U]` first
- Protocol-compliant commit formatting
- All git commands via MCP tools (obsidian_commit, obsidian_status, obsidian_push)

## Protocol Format

```
[JARVIS:OT:ENTRY_ID] Description

O = Operation: C=create, E=edit, D=delete, M=move, U=user
T = Trigger: c=conversational, a=agent
ENTRY_ID = 14-digit timestamp (journal entries only)
```

## Examples

- `[JARVIS:Cc:20260123104348]` - Create, conversational, with entry ID
- `[JARVIS:Ea]` - Edit, agent-triggered, no entry ID
- `[JARVIS:U]` - User changes (auto-detected)

## Available MCP Tools

| Tool | Purpose |
|------|---------|
| `obsidian_commit` | Create protocol-compliant commit |
| `obsidian_status` | Check working tree state |
| `obsidian_push` | Push to remote |
| `obsidian_parse_last_commit` | Verify last commit |
| `obsidian_rewrite_commit_messages` | Clean commit history |

## When to Delegate

After ANY file operation:
- Creating a journal entry → after user approves
- Moving inbox files → after moves complete
- Editing vault notes → after changes saved
- Deleting files → after deletion confirmed
