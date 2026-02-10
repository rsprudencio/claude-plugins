# Jarvis - AI Assistant Plugin for Claude Code

Jarvis is a context-aware AI assistant that manages your personal knowledge vault, maintains a git-audited trail of all operations, and integrates with Todoist for task management.

## Installation

**Recommended** (interactive setup with prerequisites check):

```bash
curl -fsSL https://raw.githubusercontent.com/rsprudencio/claude-plugins/refs/heads/master/install.sh | bash
```

**Manual** (plugin only, then configure inside Claude):

```bash
claude plugin marketplace add rsprudencio/claude-plugins
claude plugin install jarvis@raph-claude-plugins
# Then inside Claude: /jarvis-settings
```

## Quick Start

Once installed, try these five things:

1. **Orient yourself** - `jarvis "orient me"` - Get a strategic briefing
2. **Journal a thought** - `jarvis "journal this: I decided to use Rust for the CLI"` - Create a timestamped entry
3. **Search your vault** - `jarvis "/jarvis-recall project architecture"` - Semantic search by meaning
4. **Check settings** - `/jarvis-settings` - View and tune your configuration
5. **Sync Todoist** - `/jarvis-todoist` - Route inbox items to vault or Todoist projects

## Skills Reference

### Core (jarvis)

| Skill | Description |
|-------|-------------|
| `/jarvis:jarvis` | Activate Jarvis identity and load strategic context |
| `/jarvis:jarvis-settings` | View and update configuration |
| `/jarvis:jarvis-journal` | Create journal entries with vault linking |
| `/jarvis:jarvis-inbox` | Process and organize vault inbox items |
| `/jarvis:jarvis-recall` | Semantic search across vault content |
| `/jarvis:jarvis-promote` | Browse and promote auto-captured observations |
| `/jarvis:jarvis-memory-stats` | Memory system health and statistics |
| `/jarvis:jarvis-schedule` | Manage scheduled Jarvis actions |

### Todoist (jarvis-todoist) - Optional

| Skill | Description |
|-------|-------------|
| `/jarvis-todoist:jarvis-todoist` | Sync Todoist inbox with smart routing |
| `/jarvis-todoist:jarvis-todoist-setup` | Configure Todoist routing rules |

### Strategic (jarvis-strategic) - Optional

| Skill | Description |
|-------|-------------|
| `/jarvis-strategic:jarvis-orient` | Strategic briefing for session start |
| `/jarvis-strategic:jarvis-catchup` | Catch-up after time away |
| `/jarvis-strategic:jarvis-summarize` | Periodic summaries and reflection |
| `/jarvis-strategic:jarvis-patterns` | Deep behavioral analysis |

## Configuration

Config lives at `~/.jarvis/config.json`. Run `/jarvis-settings` to manage it interactively.

The default config template ships with the plugin at `defaults/config.json`.

## Need Help?

Ask Jarvis: *"What can you do?"* - Jarvis can read `capabilities.json` for a comprehensive feature reference.

Report issues: [github.com/rsprudencio/claude-plugins](https://github.com/rsprudencio/claude-plugins/issues)
