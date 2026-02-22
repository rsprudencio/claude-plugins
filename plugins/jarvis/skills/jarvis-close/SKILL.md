---
name: jarvis-close
description: Session closure checklist. Use when user says "/jarvis-close",
  "close session", "am I done", "anything left", "ready to close",
  or "session review".
user_invocable: true
---

# /jarvis-close - Session Closure Checklist

Run a structured checklist against the current session state and surface anything that needs attention before closing. Read-only — reports findings but never auto-fixes.

## Execution Flow

### 1. Mechanical checks (parallel)

Run these 5 checks in parallel using Bash and TaskList:

| Check | Command | Status Logic |
|-------|---------|-------------|
| Uncommitted changes | `git status --short` | CLEAR if empty or all changes are outside session scope; ACTION NEEDED if modified/untracked files relate to session work |
| Unpushed commits | `git log @{u}..HEAD --oneline` | CLEAR if empty; ACTION NEEDED if commits exist |
| Unpushed tags | `git push --tags --dry-run 2>&1` | CLEAR if "Everything up-to-date"; ACTION NEEDED otherwise |
| Task list | `TaskList` tool | CLEAR if no pending/in_progress tasks; ACTION NEEDED if any remain |
| Container health | `curl -sf localhost:8741/health` | CLEAR if healthy + version matches `plugins/jarvis/.claude-plugin/plugin.json`; ACTION NEEDED if down or version mismatch |

**Scope filtering for uncommitted changes:** Use conversation context to determine which files were part of this session's work. Pre-existing untracked files (like `docs/` or `.claude/worktrees/`) that were never touched during the session should NOT be flagged.

### 2. Mechanical checks (dependent)

Using results from step 1, run these additional checks:

| Check | How | Status Logic |
|-------|-----|-------------|
| Version bump | If `git status` shows modified files under `plugins/`, compare the version in `plugins/jarvis/.claude-plugin/plugin.json` against `git show HEAD:plugins/jarvis/.claude-plugin/plugin.json` | CLEAR if no plugin changes or version already bumped; ACTION NEEDED if plugin files changed without bump |
| Doc staleness | If plugin changes detected, check whether `system_prompt.py`, `capabilities.json`, or relevant `SKILL.md` files were also modified | CLEAR if no plugin changes or docs were updated; ACTION NEEDED if plugin behavior changed without doc updates |
| Tests current | Review conversation for the most recent test run (pytest output). Check if any code files were modified after that test run. | CLEAR if tests ran after last code change; ACTION NEEDED if code changed since last test run or no tests were run |

### 3. Context review (reflective)

Jarvis reviews its own conversation context. **No tools needed** — use the conversation history directly.

| Check | What to look for | Status Logic |
|-------|-----------------|-------------|
| Deferred work | Scan conversation for phrases like "let's do that later", "TODO", "next session", "deferred", "follow-up", "we should also", "park this" | List any deferred items found |
| Open commitments | Scan for things Jarvis said it would do ("I'll update", "let me also", "we still need to") that may not have been completed | List any unresolved commitments |
| Learnings worth memorizing | Review session for discoveries, conventions, or decisions valuable across sessions that weren't stored in Jarvis memory or MEMORY.md | Suggest storing if any found |
| MEMORY.md accuracy | Read `~/.claude-personal/projects/-Users-raphaelprudencio-0x88-jarvis-plugin/memory/MEMORY.md` and check if session work invalidated anything (session state, version numbers, next priority, completed work) | CLEAR if consistent; ACTION NEEDED if stale entries found |

### 4. Present summary

Format results as two tables plus action items:

```
## Session Review

### Mechanical Checks
| Check | Status |
|-------|--------|
| Uncommitted changes | CLEAR |
| Unpushed commits | 2 commits ahead of origin |
| Unpushed tags | v2.0.3 not pushed |
| Task list | CLEAR |
| Version bump | CLEAR |
| Documentation | CLEAR |
| Container health | OK (v2.0.3) |
| Tests current | CLEAR |

### Context Review
| Check | Status |
|-------|--------|
| Deferred work | None found |
| Open commitments | None found |
| Learnings to store | 1 suggestion |
| MEMORY.md accuracy | 1 stale entry |

### Action Items
1. Push 2 commits and 1 tag: `git push && git push --tags`
2. Consider storing learning: "[brief description]"
3. Update MEMORY.md "Session State" — version is now 2.0.3

### Verdict
> 3 items need attention. Address or acknowledge before closing.
```

When **all checks are CLEAR**:

```
## Session Review

All checks passed — session is ready to close.
```

### 5. User resolution

If there are action items, ask how to proceed:

```
AskUserQuestion:
  questions:
    - question: "How would you like to handle these items?"
      header: "Resolve"
      options:
        - label: "Fix now"
          description: "Address action items before closing"
        - label: "Acknowledge & close"
          description: "I see the items but I'm closing anyway"
        - label: "Defer to next session"
          description: "I'll handle these next time"
      multiSelect: false
```

If "Fix now": work through each action item with the user.
If "Acknowledge & close" or "Defer to next session": confirm session is ready to close.

## Key Rules

- **Read-only** — the skill only reports, never auto-fixes, auto-commits, or auto-pushes
- **No false alarms** — untracked files outside the session's scope should not be flagged; use conversation context to determine what's in-scope
- **No spawning agents** — Jarvis has full conversation context; use it directly for all checks
- **Concise** — table format, not paragraphs; details only in action items
- **Actionable** — each ACTION NEEDED item includes a concrete suggestion (e.g., the exact command to run)
- **Context-aware** — reflective checks use Jarvis's own conversation memory, not external tools
- **Non-blocking** — if a check fails to run (e.g., container is down, no remote tracking branch), report the failure gracefully and continue with other checks
