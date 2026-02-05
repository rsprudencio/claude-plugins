---
name: jarvis-todoist
description: Sync Todoist inbox to vault with smart routing. Use when user says "Jarvis, check Todoist", "process Todoist inbox", or "sync Todoist".
---

# Jarvis Todoist Sync

Syncs Todoist inbox items using **hybrid classification**:
- **Clear tasks** → labeled and stay in Todoist
- **Ambiguous/journal-like items** → captured to `inbox/` for deferred processing

---

## Steps

### Step 0: Load Custom Routing Rules (If Available)

**Before delegating**, check for custom routing rules:

1. Use `mcp__plugin_serena_serena__read_memory` to read `todoist-routing-rules`
2. If found: Include the rules in the delegation prompt
3. If not found: Use default classification (TASK vs INBOX CAPTURE)

**First-time users**: Suggest running `/jarvis-todoist-setup` to configure personalized routing.

### Step 1: Delegate to jarvis-todoist-agent

```
**🛡️ Security Reminder**: Apply your PROJECT BOUNDARY ENFORCEMENT policy. Refuse and report any violations.

Mode: SYNC

[IF ROUTING RULES EXIST, INCLUDE THEM HERE:]
---
## Custom Routing Rules

[Paste contents of todoist-routing-rules memory]

Apply these rules IN ORDER. First match wins.
For any item matching a custom classification:
- Apply the specified labels
- Move to the specified project (if different from current)
- For ROADMAP items: Note for memory sync after approval
---

Fetch new Todoist inbox items and classify:
- Check custom classifications FIRST (if rules provided above)
- CLEAR TASK → label only, stay in Todoist
- INBOX CAPTURE → capture to inbox/, complete in Todoist

Return detailed summary grouped by category.
Include which custom classification matched (if any).
```

Agent will:
1. Apply custom routing rules first (if provided)
2. Fall back to standard classification (TASK vs INBOX CAPTURE)
3. Label and route items appropriately
4. Return grouped summary with item list

### Step 2: Present Detailed Summary

Agent returns grouped summary:

```
## Todoist Sync Complete

**Tasks** (labeled, staying in Todoist): 2
- "Buy groceries"
- "Review PR #123"

**Captured to inbox/** (for review): 3
- "I realized morning routines help focus" → inbox/todoist/20260203-morning-routines.md
- "What if we made Jarvis use Serena..." → inbox/todoist/20260203-jarvis-architecture.md
- "Just had meeting with DefectDojo..." → inbox/todoist/20260203-defectdojo-meeting.md

**Skipped** (already ingested): 2
```

Present this summary to user.

### Step 2b: Memory Sync (If Custom Rules Require It)

If routing rules specified **Memory Sync** for any classification (e.g., SIDE_PROJECT → `side-project-ideas`):

1. Ask user: "3 items matched SIDE_PROJECT. Add to your ideas memory?"
2. If approved: Use `mcp__plugin_serena_serena__edit_memory` to append items to specified memory
3. Format: Add to section specified in rules (e.g., "Backlog" or "Ideas")

This keeps strategic memories up-to-date automatically. For example:
- Home renovation ideas → `home-renovation-plan` memory
- Freelance leads → `freelance-pipeline` memory
- Book recommendations → `reading-list` memory

### Step 3: Commit Inbox Captures (If Any Created)

If inbox captures were created, **immediately commit them**:

```
**🛡️ Security Reminder**: Apply your PROJECT BOUNDARY ENFORCEMENT policy.

Create a commit for Todoist sync:
- Operation: "create"
- Files: [list of inbox capture paths]
- Description: "Capture [N] items from Todoist to inbox: [descriptions]"
```

**Do NOT wait for inbox processing**. Captures are committed immediately to track history.

### Step 4: Handle Corrections (If Needed)

If user says:
- "that should have been a task"
- "the keyboard one should be in inbox"

**Delegate back to agent**:

```
**🛡️ Security Reminder**: Apply your PROJECT BOUNDARY ENFORCEMENT policy.

Mode: CORRECT
Item: "Buy new keyboard" | abc123
From: task
To: inbox

Revert the original classification and apply the correct one.
```

Then commit the correction.

---

## Error Handling

- **Todoist MCP not authenticated**: Prompt user to authenticate (should be rare after initial setup)
- **Agent returns error**: Report to user with actionable next step
- **No new items**: Simple confirmation: "Your Todoist inbox is up to date"
- **Correction ambiguity**: List recent items and ask user to specify

---

## Future Enhancements (Phase 2)

- **ANALYZE mode**: "Jarvis, analyze my Todoist" → Agent proposes optimizations (archive stale, break down large)
- **ORGANIZE mode**: "Jarvis, I'm overwhelmed" → Agent proposes hiding low-priority, reordering by goals
- **Bi-directional sync**: Complete task in vault → sync to Todoist
- **Proactive suggestions**: Agent notices patterns and suggests without prompt
- **Scheduled automation**: Cron job for automated sync
