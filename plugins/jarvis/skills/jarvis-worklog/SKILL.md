---
name: jarvis-worklog
description: Review what you worked on today or this week. Use when user says "/jarvis-worklog", "what did I work on today", "show my worklog", "work summary", or "weekly activity".
user_invocable: true
---

# /jarvis-worklog - Activity Review

Review auto-captured worklog entries that track what you worked on, grouped by workstream.

## Execution Flow

### 1. Parse user intent

Determine time range from input:
- `/jarvis-worklog` or `/jarvis-worklog today` → **Today** (default)
- `/jarvis-worklog week` → **This week** (last 7 days)
- `/jarvis-worklog <N>d` → **Last N days**

### 2. Query worklog entries

Use `jarvis_retrieve` to list worklog entries:

```
jarvis_retrieve(list_type="tier2", type_filter="worklog")
```

Filter results by `created_at` metadata within the requested date range.

### 3. Group by workstream

Organize entries by their `workstream` metadata field. For each workstream, show:
- Workstream name
- Number of entries
- Timeline of task summaries with timestamps
- Activity types breakdown

### 4. Present results

Format as a clean summary:

```
## Today's Activity

### VMPulse (3 entries)
- 09:15 — Investigating log errors after cluster-2 alerts [debugging]
- 11:30 — Adding retry logic to VMPulse metric collector [coding]
- 14:45 — Reviewing PR #42 for metric dashboard changes [reviewing]

### Jarvis Plugin (1 entry)
- 16:00 — Adding worklog auto-journal feature [coding]

### misc (1 entry)
- 13:00 — Setting up new SSH keys for staging server [configuring]
```

### 5. Offer follow-up actions

```
AskUserQuestion:
  questions:
    - question: "What would you like to do with this worklog?"
      header: "Action"
      options:
        - label: "Create journal entry"
          description: "Turn today's worklog into a formatted journal entry"
        - label: "View different period"
          description: "Look at a different date range"
        - label: "Done"
          description: "No further action needed"
      multiSelect: false
```

If user chooses "Create journal entry", delegate to `jarvis-journal-agent` with the worklog data as context.

## Notes

- Worklog entries are auto-captured by the stop hook during normal sessions
- Each entry has: task_summary, workstream, activity_type, tags, project context
- Workstreams emerge organically from usage — no manual setup needed
- The "misc" workstream catches one-off tasks that don't fit a project
