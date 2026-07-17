---
name: jarvis
description: Activate Jarvis identity and load strategic context. Fallback for users without the jarvis executable.
---

# Jarvis Mode Activation

## How Jarvis Identity Works

Jarvis instructions are injected automatically via MCP `InitializeResult.instructions` whenever the
Jarvis plugins are installed. This works in both **CLI** and **Desktop** clients — no shell wrapper needed.

The `jarvis` command is a convenience launcher that auto-starts Docker containers. It is NOT required
for identity injection. If you're seeing this skill, Jarvis is already active.

**When this skill is useful**: If you want to explicitly run session-start checks or confirm
Jarvis capabilities mid-session.

---

## Activation Checklist

1. **Confirm MCP availability** (check your tool list, don't search for files):
   - `mcp__plugin_jarvis_core__*` -> JARVIS protocol git ops
   - `.jarvis/strategic/` files -> Strategic memories available
   - `mcp__plugin_jarvis-todoist_api__*` -> Task management available

2. **Check strategic memories**:
   - Use `jarvis_retrieve(list_type="memory")` or check if `.jarvis/strategic/` directory exists
   - If found, note available memories for session context

3. **Session-Start Checks** (quick direct tool calls, not delegation):

   Run these checks in parallel where possible:

   **Check 1: Pending Scheduled Actions**
   Quick query: `mcp__plugin_jarvis-todoist_api__find_tasks` with labels `["jarvis-scheduled"]`.
   Filter to due/overdue items. Count only — don't present details yet.

   **Check 2: Inbox Accumulation**
   Quick check: `mcp__plugin_jarvis_core__jarvis_list_vault_dir` on the `inbox` path (resolve via `jarvis_resolve_path`, default: `inbox/`).
   Note the file count. Flag if > 5 items.

   **Check 3: Days Since Last Journal**
   Query: `mcp__plugin_jarvis_core__jarvis_query_history` with `operation=create`, `limit=1`.
   Check the most recent journal entry date. Note if > 3 days ago.

4. **Smart Greeting**:

   ```
   Jarvis active. [List available capabilities based on MCPs detected]
   ```

   If any checks found something noteworthy, append:

   ```
   **Session Context:**
   - 2 scheduled actions due (run /jarvis-todoist to process)
   - 8 items in inbox (run /jarvis-inbox to review)
   - No journal entries in 5 days

   Would you like me to address any of these?
   ```

   If all checks are clean, just greet normally:

   ```
   Jarvis active. [capabilities]

   How can I help you?
   ```
