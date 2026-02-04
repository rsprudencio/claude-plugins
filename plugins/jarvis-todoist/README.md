# Jarvis Todoist Plugin

**Version:** 1.0.0
**Author:** Raphael Prudencio
**License:** CC BY-NC 4.0

Todoist integration for Jarvis AI Assistant - smart task management and inbox capture.

---

## Features

- **Smart Task Routing:** Intelligent classification and routing of tasks
- **Inbox Capture:** Quick capture workflow for tasks
- **Todoist Sync:** Bi-directional sync with Todoist
- **Task Management Agent:** Autonomous agent for task analysis and organization

---

## Requirements

This plugin requires:

1. **Jarvis Core Plugin**
   ```bash
   claude plugin install jarvis@raph-claude-plugins
   ```

2. **Todoist MCP Server**
   - Configure Todoist MCP in your Claude Code settings
   - Get your Todoist API token from https://todoist.com/prefs/integrations

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

The agent uses Jarvis core tools for vault access and the Todoist MCP for task operations. No additional configuration needed beyond installing dependencies.

---

## Support

- **Issues:** [GitHub Issues](https://github.com/rsprudencio/claude-plugins/issues)
- **Documentation:** See `agents/jarvis-todoist-agent.md` and `skills/jarvis-todoist/SKILL.md`
