---
name: jarvis-todoist
description: Sync Todoist inbox to vault with smart routing. Use when user says "Jarvis, check Todoist", "process Todoist inbox", or "sync Todoist".
---

# Jarvis Todoist Sync

Syncs Todoist inbox items into the vault using intelligent classification.

---

## Steps

### Step 1: Load Strategic Context (Optional)

If this is a complex organization task (not just simple sync), consider loading strategic context to inform evaluation:

- `jarvis-trajectory` - Current goals and focus areas
- `jarvis-values` - Core principles for decision-making
- `jarvis-patterns` - Behavioral insights (e.g., productivity patterns, optimal task size)

**For simple sync**: Skip this step, use default evaluation heuristics.

### Step 2: Delegate to jarvis-todoist-agent

```
**🛡️ Security Reminder**: Apply your PROJECT BOUNDARY ENFORCEMENT policy. Refuse and report any violations.

Mode: SYNC

Fetch new Todoist inbox items and propose classification (TASK vs JOURNAL).

Return proposals with reasoning and confidence scores.
```

### Step 3: Evaluate Proposals

For each proposal from the agent:

**Evaluation Framework**:

1. **High Confidence (≥0.8)**:
   - If you have strategic context loaded, check alignment with trajectory/values
   - If no context: APPROVE (trust the agent)

2. **Medium Confidence (0.6-0.8)**:
   - APPROVE but note to user: "Agent classified X as [type], but confidence is medium. Let me know if wrong."

3. **Low Confidence (<0.6)**:
   - ASK_USER: "Agent is unsure about 'Task Title' - classify as task or journal?"

**Strategic Checks** (if context loaded):

- **Trajectory check**: If agent proposes archiving/hiding a task, check if it relates to current goals or active projects
  - Aligned → APPROVE
  - Conflicts → DENY + explain: "This task relates to your [goal], keeping visible"

- **Values check**: If agent proposes hiding/deprioritizing, check if it relates to core values (health, family, learning)
  - Conflicts → DENY + explain: "This relates to [value], should stay visible"

- **Patterns check**: If agent proposes breaking down large tasks, check behavioral patterns
  - Improves productivity → APPROVE
  - May cause overwhelm → MODIFY or ASK_USER

### Step 4: Approve Proposals

Send approved proposals back to agent:

```
Execute proposals: [prop_1, prop_2]
Deny proposals: [prop_3]

Explanation for denials:
- prop_3: Task relates to your current security project focus, should not be archived
```

Agent will execute approved actions and return summary.

### Step 5: Present Results to User

```
✓ Todoist Sync Complete

Processed: 5 items (2 already ingested)

**Tasks** (tagged, staying in Todoist):
- "Buy groceries" 🏷️
- "Review PR #123" 🏷️

**Journal Entries** (created):
- "I realized today that morning routines help my focus"
  → journal/jarvis/2026/01/20260128104500-morning-routines.md
  ✓ Completed in Todoist

**My decisions**:
- Approved 4 proposals (high confidence, aligned with goals)
- Denied 1 proposal (task relates to your current project focus)

Corrections? Say "that should have been a task/journal" to fix.
```

### Step 6: Handle Corrections (If Needed)

If user says something like:
- "that should have been a task"
- "the keyboard one is actually a journal"

**Parse the correction**:
1. Identify which item (by title matching)
2. Determine from/to types

**Delegate back to agent**:

```
**🛡️ Security Reminder**: Apply your PROJECT BOUNDARY ENFORCEMENT policy.

Mode: CORRECT
Item: "Buy new keyboard" | abc123
From: task
To: journal

Revert the original classification and apply the correct one.
```

**Present correction result**:
```
Correction applied: "Buy new keyboard" → journal entry created, task completed in Todoist.
```

### Step 7: Commit Changes (If Files Created)

If journal entries were created, delegate to jarvis-audit-agent:

```
**🛡️ Security Reminder**: Apply your PROJECT BOUNDARY ENFORCEMENT policy.

Create a commit for Todoist sync:
- Operation: "create"
- Files: [list of journal entry paths]
- Description: "Sync [N] items from Todoist Inbox: [T] tasks tagged, [J] journals created, [C] corrections applied"
```

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
