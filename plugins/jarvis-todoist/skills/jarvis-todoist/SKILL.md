---
name: jarvis-todoist
description: Sync Todoist inbox to vault with smart routing. Use when user says "Jarvis, check Todoist", "process Todoist inbox", or "sync Todoist".
---

# Jarvis Todoist Sync

Syncs Todoist inbox items using **hybrid classification**:
- **Clear tasks** → labeled and stay in Todoist
- **Ambiguous/journal-like items** → captured to `paths.inbox_todoist` (default: `inbox/todoist/`) for deferred processing

---

## Steps

### Step 0: Check Scheduled Actions

Check if any recurring Jarvis actions are due (these are pre-configured by the user via `/jarvis-schedule`):

1. **Delegate to agent**:
   ```
   **🛡️ Security Reminder**: Apply your PROJECT BOUNDARY ENFORCEMENT policy.

   Mode: SCHEDULED
   ```

2. **If scheduled actions are due**:
   - Present the list to the user (sorted: most overdue first)
   - Ask: "You have [N] scheduled actions due. Run them now?"
   - If approved: Execute each action by invoking the corresponding skill, then complete the Todoist task
   - If declined: Note them and proceed to sync

3. **If no actions due**: Proceed silently to Step 0b.

### Step 0b: Load Custom Routing Rules (If Available)

**Before delegating SYNC**, check for custom routing rules:

1. Use `jarvis_memory_read("todoist-routing-rules")` to read routing rules
   (stored at `.jarvis/strategic/todoist-routing-rules.md`)
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
- INBOX CAPTURE → capture to paths.inbox_todoist (configurable), complete in Todoist

Return detailed summary grouped by category.
Include which custom classification matched (if any).
```

Agent will:
1. Apply custom routing rules first (if provided)
2. Fall back to standard classification (TASK vs INBOX CAPTURE)
3. Label and route items appropriately
4. Return grouped summary with item list

### Step 1b: Review or Defer

After the agent returns its proposals, ask the user:

> "Review items now or defer to inbox?"

- **Review now**: Present item-by-item classification (user can stop anytime with "stop review" — remaining items go to inbox). Each item gets the full 6 classification options (see `/jarvis-inbox`): Journal entry, Work note, Personal note, Person/contact, Discard, Skip.
- **Defer**: All ambiguous items go straight to `paths.inbox_todoist` (default: `inbox/todoist/`) without item-by-item review. Tasks still get labeled in Todoist as usual. This is the "silent" path — no interruption, inbox captures committed in bulk.

### Step 2: Present Detailed Summary

Agent returns grouped summary:

```
## Todoist Sync Complete

**Tasks** (labeled, staying in Todoist): 2
- "Buy groceries"
- "Review PR #123"

**Captured to inbox** (for review): 3
- "I realized morning routines help focus" → {paths.inbox_todoist}/20260203-morning-routines.md (configurable)
- "What if we made Jarvis modular..." → {paths.inbox_todoist}/20260203-jarvis-architecture.md
- "Just had meeting with DefectDojo..." → {paths.inbox_todoist}/20260203-defectdojo-meeting.md

**Skipped** (already ingested): 2
```

Present this summary to user.

### Step 2b: Memory Sync (If Custom Rules Require It)

If routing rules specified **Memory Sync** for any classification (e.g., SIDE_PROJECT → `side-project-ideas`):

1. Ask user: "3 items matched SIDE_PROJECT. Add to your ideas memory?"
2. If approved: Read the target memory with `jarvis_memory_read(memory_name)`, append the new items to the appropriate section, then write back with `jarvis_memory_write(memory_name, updated_content, overwrite=true)`
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

## Future Enhancements

- **ANALYZE mode**: "Jarvis, analyze my Todoist" → Agent proposes optimizations (archive stale, break down large)
- **ORGANIZE mode**: "Jarvis, I'm overwhelmed" → Agent proposes hiding low-priority, reordering by goals
- **Bi-directional sync**: Complete task in vault → sync to Todoist
- **Proactive suggestions**: Agent notices patterns and suggests without prompt
