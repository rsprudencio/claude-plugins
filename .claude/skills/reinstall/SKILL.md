---
description: Reinstall Jarvis plugins with cache clear
---

# Jarvis Plugin Reinstall Workflow

You are helping the developer reinstall Jarvis plugins during active development.

## Modular Architecture

The plugin is split into 3 independent plugins in the `raph-claude-plugins` marketplace:

| Plugin | Cache Path |
|--------|------------|
| `jarvis` | `~/.claude/plugins/cache/raph-claude-plugins/jarvis/` |
| `jarvis-todoist` | `~/.claude/plugins/cache/raph-claude-plugins/jarvis-todoist/` |
| `jarvis-strategic` | `~/.claude/plugins/cache/raph-claude-plugins/jarvis-strategic/` |

## Workflow

### Step 1: Clear Cache & Reinstall All Plugins

Run the following command chain:

```bash
rm -rf ~/.claude/plugins/cache/raph-claude-plugins/* && \
claude plugin marketplace update && \
claude plugin uninstall jarvis@raph-claude-plugins 2>/dev/null; \
claude plugin uninstall jarvis-todoist@raph-claude-plugins 2>/dev/null; \
claude plugin uninstall jarvis-strategic@raph-claude-plugins 2>/dev/null; \
claude plugin install jarvis@raph-claude-plugins && \
claude plugin install jarvis-todoist@raph-claude-plugins && \
claude plugin install jarvis-strategic@raph-claude-plugins
```

### Step 2: Remind User to Restart

**IMPORTANT:** Plugin changes only take effect after a full restart of Claude Code, not just a reload.

Inform the user:
```
Plugins reinstalled successfully

RESTART REQUIRED
Plugin changes will only take effect after you restart Claude Code.

- macOS: Cmd+Q to quit, then reopen
- Linux/Windows: Close and reopen the application
```

---

## Usage

```
/reinstall           # Reinstall all 3 plugins
```

---

## Notes

- This skill is for development only (not included in the plugin distribution)
- Never skip the restart reminder - users often forget this step
- All 3 plugins are reinstalled together to ensure consistency
