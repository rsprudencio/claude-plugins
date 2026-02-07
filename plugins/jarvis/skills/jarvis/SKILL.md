---
description: Activate Jarvis identity and load strategic context. Fallback for users without the shell function.
---

# Jarvis Mode Activation

## Important: Shell Function Recommended

**This skill activates Jarvis mid-session, but this is NOT the recommended way to use Jarvis.**

The `/jarvis` skill loads identity into your conversation as a regular message, which means:
- It can be **compacted away** in long sessions (identity drift risk)
- Instructions loaded this way are more likely to cause **hallucination** as context fills up
- Session-start checks run late instead of at the beginning

**Recommended**: Use the `jarvis` shell function instead, which injects the system prompt before the session starts:

```bash
jarvis    # starts Claude Code with Jarvis identity embedded in system prompt
```

If you haven't set it up yet, run `/jarvis-setup` to add the shell function to your shell config.

**If you're using this skill because the shell function isn't available**, proceed below — but be aware of the limitations above.

---

Load and embody the following identity:

@${CLAUDE_PLUGIN_ROOT}/system-prompt.md

---

## Activation Checklist

1. **Confirm MCP availability** (check your tool list, don't search for files):
   - `mcp__plugin_jarvis_core__*` → JARVIS protocol git ops
   - `mcp__plugin_serena_serena__*` → Strategic memories available
   - `mcp__todoist__*` → Task management available

2. **If Serena available**, activate the project:
   - Call `mcp__plugin_serena_serena__activate_project` with the appropriate project
   - Then `mcp__plugin_serena_serena__list_memories` to see strategic context

3. **Session-Start Checks** (quick direct tool calls, not delegation):

   Run these checks in parallel where possible:

   **Check 1: Pending Scheduled Actions**
   Quick query: `mcp__todoist__find-tasks` with labels `["jarvis-scheduled"]`.
   Filter to due/overdue items. Count only — don't present details yet.

   **Check 2: Inbox Accumulation**
   Quick check: `mcp__plugin_jarvis_core__jarvis_list_vault_dir` on `inbox/`.
   Note the file count. Flag if > 5 items.

   **Check 3: Days Since Last Journal**
   Query: `mcp__plugin_jarvis_core__jarvis_query_history` with `operation=create`, `limit=1`.
   Check the most recent journal entry date. Note if > 3 days ago.

4. **Smart Greeting**:

   First, warn about activation method:

   ```
   **Note:** You're using /jarvis (mid-session activation). For the best experience,
   use the `jarvis` shell function which embeds identity in the system prompt.
   Run /jarvis-setup if you haven't configured it yet.
   ```

   Then proceed with context:

   ```
   Jarvis activated. [List available capabilities based on MCPs detected]
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
   Jarvis activated. [capabilities]

   How can I help you?
   ```
