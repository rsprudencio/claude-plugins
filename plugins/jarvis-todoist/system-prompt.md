# Todoist Task Management

## Architecture

This plugin includes a built-in MCP server that wraps the Todoist API via the official
`todoist-api-python` SDK. No external HTTP MCP is needed — the local stdio transport never drops sessions.

**Configuration**: API token must be set in `~/.jarvis/config.json` → `todoist.api_token`.
Get your token at https://app.todoist.com/app/settings/integrations/developer

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
