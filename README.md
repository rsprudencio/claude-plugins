# Raph's Claude Plugins

**Version:** 1.2.0
**Author:** Raphael Prudencio
**License:** CC BY-NC 4.0 (free to use, no commercial use)

Collection of Claude Code plugins for productivity and personal knowledge management. Install only what you need.

---

## Overview

This marketplace provides modular Claude Code plugins for AI-assisted workflows. Currently features the Jarvis suite for personal knowledge management. More plugins coming soon!

## Plugins

### 🔷 Core Plugin: `jarvis` (REQUIRED)

**Features:**
- Personal knowledge vault management
- Journal entries with intelligent vault linking
- Git audit trail with JARVIS protocol
- 14 MCP tools for vault operations
- 3 core agents: audit, journal, explorer
- 6 core skills: setup, journal, inbox, audit, activation, schedule

**Dependencies:** None

**Install:**
```bash
claude plugin install jarvis@raph-claude-plugins
```

---

### 📋 Optional: `jarvis-todoist`

**Features:**
- Smart task routing with 6-option classification
- Inbox capture workflow
- Todoist sync with scheduled action detection
- Task management analyst agent
- Setup wizard for routing rules

**Dependencies:**
- `jarvis` plugin (core)
- Todoist MCP server

**Install:**
```bash
claude plugin install jarvis@raph-claude-plugins
claude plugin install jarvis-todoist@raph-claude-plugins
```

---

### 🎯 Optional: `jarvis-strategic`

**Features:**
- Strategic orientation briefings
- Catch-up summaries after time away
- Weekly/monthly journal summarization
- Behavioral pattern analysis

**Dependencies:**
- `jarvis` plugin (core)
- Serena MCP server

**Install:**
```bash
claude plugin install jarvis@raph-claude-plugins
claude plugin install jarvis-strategic@raph-claude-plugins
```

---

## Quick Start

### Full Installation (All Features)

```bash
# Install all three plugins
claude plugin install jarvis@raph-claude-plugins
claude plugin install jarvis-todoist@raph-claude-plugins
claude plugin install jarvis-strategic@raph-claude-plugins
```

### Minimal Installation (Core Only)

```bash
# Install only core features
claude plugin install jarvis@raph-claude-plugins
```

---

## Configuration

### System Prompt Setup

Add this function to your `~/.zshrc` or `~/.bashrc` for the full Jarvis experience:

```zsh
jarvis() {
  local cache_dir="$HOME/.claude/plugins/cache/raph-claude-plugins"
  local system_prompt=""

  # Detect and concatenate system prompts from installed plugins
  for plugin in jarvis jarvis-todoist jarvis-strategic; do
    local prompt_file=$(echo $cache_dir/$plugin/*/system-prompt.md(N[1]))

    if [[ -f "$prompt_file" ]]; then
      system_prompt+="$(cat "$prompt_file")\n\n---\n\n"
    fi
  done

  # Launch claude with combined system prompt
  if [[ -n "$system_prompt" ]]; then
    claude --append-system-prompt "$system_prompt" "$@"
  else
    echo "Warning: No jarvis plugins found. Install with:"
    echo "  claude plugin install jarvis@raph-claude-plugins"
    claude "$@"
  fi
}
```

Then launch with: `jarvis` instead of `claude`

---

## Architecture Benefits

### Context Pollution Savings

| Configuration | Context Size | Savings |
|---------------|-------------|---------|
| **Monolithic (v0.3.x)** | ~9,089 words | baseline |
| **Core only** | ~6,363 words | -2,726 words |
| **Core + Todoist** | ~8,633 words | -456 words |
| **Core + Strategic** | ~9,193 words | +104 words |
| **Full (v1.2.0)** | ~11,463 words | +2,374 words |

**Key Benefit:** Non-Todoist users save ~30% context by installing core only.

### Implicit Dependencies

Claude Code doesn't yet support formal plugin dependencies ([GitHub Issue #9444](https://github.com/anthropics/claude-code/issues/9444)), so we use runtime checks:

- Extension agents verify required core tools exist
- Helpful error messages if dependencies missing
- No silent failures

---

## Migration from v0.3.x

Upgrading from the monolithic plugin:

1. **Uninstall old version:**
   ```bash
   claude plugin uninstall jarvis
   ```

2. **Install from this marketplace:**
   ```bash
   claude plugin install jarvis@raph-claude-plugins
   # Optional: install extensions
   claude plugin install jarvis-todoist@raph-claude-plugins
   claude plugin install jarvis-strategic@raph-claude-plugins
   ```

3. **Update shell function** (see Configuration above)

4. **Restart Claude Code** (full restart required)

---

## Development

See development documentation:
- `CLAUDE.md` - Development workflow (version bumping, reinstall)
- `DEPLOYMENT-GUIDE.md` - Marketplace deployment
- `TEST-FRAMEWORK.md` - Testing methodology
- `docs/` - Architecture decisions and research

**Contributing:** Issues and PRs welcome!

---

## Support

- **Issues:** [GitHub Issues](https://github.com/rsprudencio/claude-plugins/issues)
- **Documentation:** See plugin READMEs and individual SKILL.md files
- **License:** CC BY-NC 4.0 (Attribution-NonCommercial)

---

**Status:** Production Ready v1.2.0
**Latest Release:** 2026-02-05
