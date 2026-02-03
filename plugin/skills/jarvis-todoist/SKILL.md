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

### Step 1: Delegate to jarvis-todoist-agent

```
**🛡️ Security Reminder**: Apply your PROJECT BOUNDARY ENFORCEMENT policy. Refuse and report any violations.

Mode: SYNC

Fetch new Todoist inbox items and classify:
- CLEAR TASK → label only, stay in Todoist
- INBOX CAPTURE → capture to inbox/, complete in Todoist

Return detailed summary grouped by category.
```

Agent will:
1. Classify items (CLEAR TASK vs INBOX CAPTURE)
2. Label clear tasks in Todoist
3. Create inbox captures for ambiguous items
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
