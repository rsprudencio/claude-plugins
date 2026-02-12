# Jarvis Todoist Plugin

**Version:** 1.4.2
**Author:** Raphael Prudencio
**License:** CC BY-NC 4.0

Todoist integration for Jarvis AI Assistant - smart task management and inbox capture.
Includes a built-in MCP server wrapping the official `todoist-api-python` SDK (local stdio, no session drops).

---

## Features

- **Built-in MCP Server:** 9 tools wrapping the Todoist API via the official SDK
- **Smart Task Routing:** Intelligent classification and routing of tasks
- **Inbox Capture:** Quick capture workflow for tasks
- **Todoist Sync:** Bi-directional sync with Todoist
- **Task Management Agent:** Autonomous agent for task analysis and organization

---

## Requirements

1. **Jarvis Core Plugin**
   ```bash
   claude plugin install jarvis@raph-claude-plugins
   ```

2. **Todoist API Token**
   - Get your token at https://app.todoist.com/app/settings/integrations/developer
   - Add to `~/.jarvis/config.json` → `todoist.api_token`

---

## Installation

```bash
# Install core first
claude plugin install jarvis@raph-claude-plugins

# Then install Todoist extension
claude plugin install jarvis-todoist@raph-claude-plugins
```

---

## Usage

### Task Management

```
You: "Jarvis, check my Todoist tasks"
Jarvis: [Delegates to jarvis-todoist-agent]
```

### Inbox Capture

Use the `/jarvis:jarvis-todoist` skill to sync and process Todoist inbox.

---

## Agent

**jarvis-todoist-agent** - Task management analyst that:
- Classifies and routes tasks
- Analyzes task relationships
- Proposes organization strategies
- Integrates with vault context

---

## Configuration

The plugin includes its own MCP server (`jarvis-todoist-api`) that communicates with Todoist via the official SDK. Configure your API token in `~/.jarvis/config.json`:

```json
{
  "todoist": {
    "api_token": "your-token-here"
  }
}
```

Run `/jarvis-settings` to configure interactively.

---

## Support

- **Issues:** [GitHub Issues](https://github.com/rsprudencio/claude-plugins/issues)
- **Documentation:** See `agents/jarvis-todoist-agent.md` and `skills/jarvis-todoist/SKILL.md`
