# Claude Development Guide - Jarvis Plugin

This file contains development instructions and conventions for Claude when working on the Jarvis plugin.

---

## Version Bumping (Required Before Reinstall)

**ALWAYS bump version in `plugin/.claude-plugin/plugin.json` before reinstalling.**

### Semantic Versioning Rules

Use the following criteria to decide bump type:

#### Patch (0.2.x → 0.2.x+1)
Use for:
- Bug fixes
- Small changes
- Documentation updates
- Minor refactoring
- Tool configuration tweaks
- Agent instruction clarifications

#### Minor (0.2.x → 0.3.0)
Use for:
- New features
- New skills/agents/commands
- Workflow changes
- Non-breaking enhancements
- New MCP integrations

#### Major (0.x.x → 1.0.0)
Use for:
- Breaking changes
- Complete workflow redesigns
- **ALWAYS ask user before major bumps**

### Current Version
Check: `plugin/.claude-plugin/plugin.json`

---

## Plugin Reinstall Workflow

When reinstalling during development (after code changes):

### Step 1: Bump Version
Edit `plugin/.claude-plugin/plugin.json` and increment version according to rules above.

### Step 2: Clean Cache & Reinstall

```bash
# Clear all cached versions
rm -rf ~/.claude/plugins/cache/jarvis-marketplace/jarvis/*

# Update marketplace
claude plugin marketplace update

# Uninstall existing
claude plugin uninstall jarvis@jarvis-marketplace

# Install fresh
claude plugin install jarvis@jarvis-marketplace
```

### Step 3: Restart Claude Code
**Required** - Plugin changes only apply after full restart (not just reload).

---

## When to Reinstall

Reinstall is required after modifying:

- **Agent definitions** - Files in `plugin/agents/*.md`
- **Skills** - Files in `plugin/skills/*/SKILL.md`
- **MCP server code** - Files in `plugin/mcp-server/`
- **Plugin manifest** - `plugin/.claude-plugin/plugin.json`
- **MCP configuration** - `plugin/.mcp.json`
- **System prompt** - `plugin/system-prompt.md`

---

## Troubleshooting Reinstalls

If plugin doesn't load after reinstall:

1. **Verify cache cleared**:
   ```bash
   ls ~/.claude/plugins/cache/jarvis-marketplace/jarvis/
   # Should only show current version
   ```

2. **Check marketplace updated**:
   ```bash
   claude plugin marketplace list
   ```

3. **Verify uninstall completed**:
   ```bash
   claude plugin list
   # Should NOT show jarvis
   ```

4. **Check for errors** in Claude Code logs

5. **Full restart** - Quit and reopen Claude Code (not just reload)

6. **Verify git state**:
   ```bash
   git status
   # Ensure changes are committed
   ```

---

## Development Notes

### Plugin Architecture

- **Agents** - Autonomous sub-agents (journal, audit, todoist)
- **Skills** - User-invocable workflows (slash commands)
- **MCP Server** - `jarvis-tools` provides vault file ops and git operations
- **System Prompt** - Jarvis identity and delegation model

### Key Files

| File | Purpose |
|------|---------|
| `plugin/.claude-plugin/plugin.json` | Plugin manifest (version, name, author) |
| `plugin/system-prompt.md` | Jarvis core identity and constraints |
| `plugin/.mcp.json` | MCP server registration |
| `plugin/mcp-server/` | Python MCP server implementation |
| `plugin/agents/*.md` | Agent definitions with tools/prompts |
| `plugin/skills/*/SKILL.md` | Skill workflows |

### Version History

- **0.3.0** - jarvis-explorer-agent (vault-aware search), test framework v1.0, capitalization fixes
- **0.2.1** - MCP rename (jarvis-tools→tools), Todoist workflow simplification, inbox processing enhancements
- **0.2.0** - Initial comprehensive test coverage, audit agent refinements

---

## Quick Commands Reference

```bash
# Reinstall (with cache clear)
rm -rf ~/.claude/plugins/cache/jarvis-marketplace/jarvis/* && \
claude plugin marketplace update && \
claude plugin uninstall jarvis@jarvis-marketplace && \
claude plugin install jarvis@jarvis-marketplace

# Check installed version
cat ~/.claude/plugins/cache/jarvis-marketplace/jarvis/*/plugin.json | grep version

# View agent configuration
cat ~/.claude/plugins/cache/jarvis-marketplace/jarvis/*/agents/jarvis-todoist-agent.md | head -10
```
