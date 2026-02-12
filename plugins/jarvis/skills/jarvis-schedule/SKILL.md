---
name: jarvis-schedule
description: Create, list, or cancel scheduled Jarvis actions. Use when user says "schedule journal refinement weekly", "show my scheduled actions", "cancel the daily orient", or "manage Jarvis schedules".
---

# Jarvis Schedule Manager

Manages recurring Jarvis actions via Todoist. Scheduled actions are Todoist recurring tasks with special labels that Jarvis detects during `/jarvis-todoist` sync (Step 0).

---

## How It Works

Todoist recurring tasks serve as the scheduling engine:
- **`jarvis-scheduled`** label marks a task as a Jarvis scheduled action
- **`jarvis-action:[action]`** label specifies which action to trigger
- Todoist handles recurrence, due dates, and its own notifications
- Jarvis detects due actions when the user runs `/jarvis-todoist` or `/jarvis:jarvis`

---

## Available Actions

| Action Label | Triggers | Description |
|-------------|----------|-------------|
| `jarvis-action:refine-journal` | Journal refinement | Scan `#status/refine` entries, extract evergreen knowledge |
| `jarvis-action:clean-memories` | Memory maintenance | Review and prune stale strategic memories |
| `jarvis-action:todoist-sync` | Todoist sync | Full inbox sync via `/jarvis-todoist` |
| `jarvis-action:pattern-check` | Pattern analysis | Run `/jarvis-patterns` (quick depth) |
| `jarvis-action:orient` | Orientation briefing | Run `/jarvis-orient` |

Users can define custom actions — any `jarvis-action:*` label will be detected.

---

## Operations

### Create Schedule

Parse the user's request and create a Todoist recurring task:

1. Identify the action (map to `jarvis-action:*` label)
2. Parse the recurrence ("weekly", "every Monday", "daily at 9am")
3. Create the task:
   ```
   Use mcp__plugin_jarvis_todoist_api__add_tasks:
   - content: "[Description from user]"
   - labels: ["jarvis-scheduled", "jarvis-action:[action]"]
   - dueString: "[natural language recurrence]"
   ```
4. Confirm creation with next due date

**Example interaction:**
```
User: "Schedule journal refinement every Sunday evening"

Creating scheduled action:
- Task: "Journal refinement"
- Action: jarvis-action:refine-journal
- Recurrence: Every Sunday at 7pm
- Labels: jarvis-scheduled, jarvis-action:refine-journal

Created. Next due: Sunday Feb 9 at 7:00 PM.
```

### List Schedules

Query all tasks with the `jarvis-scheduled` label and present as a table:

```
Use mcp__plugin_jarvis_todoist_api__find_tasks:
- labels: ["jarvis-scheduled"]
- limit: 50
```

Present results:

```
| # | Action | Task | Next Due | Overdue? |
|---|--------|------|----------|----------|
| 1 | refine-journal | "Journal refinement" | Sun Feb 9 | No |
| 2 | orient | "Daily orientation" | Tomorrow | No |
| 3 | todoist-sync | "Weekly Todoist sync" | Mon Feb 3 | Yes (2d) |
```

### Cancel Schedule

**Delete** the Todoist task (do NOT complete — completing triggers recurrence):

```
Use mcp__plugin_jarvis_todoist_api__delete_object:
- type: "task"
- id: "[task_id]"
```

If the user says "cancel the journal refinement schedule":
1. Search for tasks with `jarvis-scheduled` + `jarvis-action:refine-journal`
2. Confirm with user before deleting
3. Delete the task
4. Confirm: "Cancelled. Journal refinement will no longer be scheduled."

### Pause Schedule (Temporary)

To pause without cancelling:
1. Remove the `jarvis-scheduled` label (keeps the task but Jarvis won't detect it)
2. User can re-add the label later to resume

---

## Notes

- This skill lives in the core jarvis plugin because scheduling is cross-cutting (not Todoist-specific in concept, even though Todoist is the current engine)
- Detection of due actions happens in `/jarvis-todoist` Step 0 and `/jarvis:jarvis` session-start checks
- Todoist sends its own push notifications for due tasks, so users get reminded even when Claude Code is closed
