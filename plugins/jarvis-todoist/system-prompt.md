# Todoist Task Management

## Task Routing

When the user mentions tasks, todos, or Todoist:
- Delegate to `jarvis-todoist-agent` for task management and analysis
- Agent handles classification, routing, and inbox capture
- Integrates with vault via core tools

## Task Classification Heuristics

The todoist agent determines task routing based on:
- Task context and metadata
- Project relevance and vault connections
- Due dates, deadlines, and priorities
- User's strategic focus areas

## Inbox Capture Workflow

Use `/jarvis:jarvis-todoist` skill when user wants to process Todoist inbox or sync tasks.
