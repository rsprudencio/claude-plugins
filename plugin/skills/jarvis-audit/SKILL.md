---
name: jarvis-audit
description: Git audit protocol reference. Background knowledge for JARVIS Protocol commits.
user-invocable: false
---

# JARVIS Protocol - Git Audit System

Every file operation creates an auditable git commit via `jarvis-audit-agent`.

## Delegation Pattern

**You never run git commands directly. Always delegate to `jarvis-audit-agent`.**

```json
{
  "operation": "create|edit|delete|move|user",
  "description": "what was done",
  "entry_id": "YYYYMMDDHHMMSS",
  "files": ["path/to/file.md"]
}
```

## Agent Handles

- Detecting uncommitted user changes (auto-commits as `[JARVIS:U]` first)
- Protocol-compliant commit formatting
- All git commands via MCP tools (jarvis_commit, jarvis_status, jarvis_push)

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
| `jarvis_commit` | Create protocol-compliant commit |
| `jarvis_status` | Check working tree state |
| `jarvis_push` | Push to remote |
| `jarvis_parse_last_commit` | Verify last commit |
| `jarvis_rewrite_commit_messages` | Clean commit history |

## When to Delegate

After ANY file operation:
- Creating a journal entry → after user approves
- Moving inbox files → after moves complete
- Editing vault notes → after changes saved
- Deleting files → after deletion confirmed
