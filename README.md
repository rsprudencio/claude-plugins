# Jarvis - Claude Code Plugin Marketplace

**Version:** 1.14.0
**Author:** Raphael Prudencio
**License:** CC BY-NC 4.0 (free to use, no commercial use)

Personal AI assistant plugins for Claude Code with knowledge vault management, semantic memory, and strategic context awareness.

---

## Features

- **Knowledge vault management** - PKM system with bidirectional linking
- **Git-audited trail** - JARVIS protocol commits for all vault operations
- **Semantic memory** - Two-tier ChromaDB architecture with auto-indexing
- **Auto-extract** - Passive observation capture from conversations
- **Journal workflow** - Daily notes and Jarvis entries with intelligent vault linking
- **Strategic context** - Goal tracking, value alignment, pattern analysis
- **Todoist integration** - Smart task routing with project classification
- **21 MCP tools** - Python-based MCP server with comprehensive vault access
- **3 core agents** - Journal, audit, and explorer with specialized capabilities
- **10 core skills** - setup, journal, inbox, audit, activation, schedule, recall, memory-index, memory-stats, promote

---

## Installation

### Prerequisites

- **Claude Code CLI** (latest version)
- **Python 3.10+** (for MCP server)
- **uv** (Python package manager for reproducible builds)

### Install Plugins

```bash
# Add the marketplace (first time only)
claude plugin marketplace add raph-claude-plugins https://github.com/rsprudencio/claude-plugins

# Install core plugin (required)
claude plugin install jarvis@raph-claude-plugins

# Optional extensions
claude plugin install jarvis-todoist@raph-claude-plugins
claude plugin install jarvis-strategic@raph-claude-plugins
```

### First-Time Setup

Run the interactive setup wizard:

```bash
/jarvis-setup
```

This configures:
- Vault path location
- Shell integration (optional)
- Auto-extract mode (passive observation capture)
- Memory indexing

---

## Plugins

### 🔷 Core Plugin: `jarvis` (REQUIRED)

**Features:**
- Personal knowledge vault management
- Journal entries with intelligent vault linking
- Git audit trail with JARVIS protocol
- Semantic memory with two-tier ChromaDB architecture
- Auto-extract: passive observation capture from conversations
- 21 MCP tools for vault operations
- 3 core agents: audit, journal, explorer
- 10 core skills: setup, journal, inbox, audit, activation, schedule, recall, memory-index, memory-stats, promote

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

**Install:**
```bash
claude plugin install jarvis-strategic@raph-claude-plugins
```

---

## Key Workflows

### Journal Entries

Create journal entries with intelligent vault linking:

```bash
/jarvis-journal
```

The journal agent:
- Searches vault for relevant context
- Suggests bidirectional links
- Auto-indexes entries into semantic memory
- Commits via JARVIS protocol

### Semantic Search

Search vault content by meaning (not just keywords):

```bash
/recall [query]
```

Example:
```bash
/recall "what did I decide about the TypeScript migration?"
```

Uses ChromaDB with two-tier architecture:
- **Tier 1**: File-backed vault content (permanent)
- **Tier 2**: Auto-extracted observations (ephemeral, promotable)

### Strategic Briefings

Get oriented at session start:

```bash
/jarvis-orient
```

Loads strategic context:
- Current priorities and goals
- Active projects
- Recent journal themes

### Task Management

Process Todoist inbox with smart routing:

```bash
/jarvis-todoist
```

Routes tasks based on:
- Project relevance
- Vault connections
- Due dates and priorities
- Strategic focus areas

---

## Memory System

### ChromaDB Semantic Memory

Jarvis uses ChromaDB for intelligent semantic search across your vault:

- **Embedding Model**: all-MiniLM-L6-v2 (384d)
- **Storage**: `~/.jarvis/memory_db/` (outside vault to avoid sync pollution)
- **Indexing**: Auto-index on journal creation, bulk index with `/memory-index`

### Two-Tier Architecture

**Tier 1: File-Backed (Permanent)**
- User-created content (vault files, strategic memories)
- Git-tracked, Obsidian-visible, permanent
- Namespace prefix: `vault::`, `memory::`

**Tier 2: ChromaDB-First (Ephemeral)**
- Auto-generated content (observations, patterns, summaries)
- ChromaDB-only, invisible to Obsidian, disposable
- Namespace prefixes: `obs::`, `pattern::`, `summary::`, `code::`, `rel::`, `hint::`, `plan::`
- Can be promoted to Tier 1 when important

**Content Types (Tier 2):**
- `observation` - Captured insights from conversations
- `pattern` - Detected behavioral patterns
- `summary` - Session or period summaries
- `code` - Code snippets and analysis
- `relationship` - Entity relationship mappings
- `hint` - Contextual hints and suggestions
- `plan` - Task plans and strategies

### Auto-Extract

Passive observation capture from conversations:

- **Stop hook**: Runs after each conversation turn
- **Filtering**: Anti-recursion skip lists, SHA-256 dedup
- **Modes**: disabled, background-api, background-cli
- **Smart extraction**: Uses Haiku to identify valuable insights
- **Promotion**: Important observations can be promoted to permanent vault files

Manage Tier 2 content:

```bash
/promote  # Browse and promote ephemeral observations
```

### Memory Commands

```bash
/recall [query]          # Semantic search across vault
/memory-index            # Bulk index vault files into ChromaDB
/memory-stats            # Show memory system health and stats
/promote                 # Browse and promote Tier 2 content
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

## Development

### Repository Structure

```
.
├── .claude-plugin/
│   └── marketplace.json          # Marketplace manifest
├── plugins/
│   ├── jarvis/                   # Core plugin
│   │   ├── .claude-plugin/       # Plugin manifest
│   │   ├── agents/               # Journal, audit, explorer agents
│   │   ├── skills/               # Core skills
│   │   ├── mcp-server/           # Python MCP server (21 tools)
│   │   ├── system-prompt.md      # Jarvis identity
│   │   └── .mcp.json             # MCP registration
│   ├── jarvis-todoist/           # Todoist extension
│   └── jarvis-strategic/         # Strategic analysis extension
├── CLAUDE.md                     # Development guide
└── LICENSE                       # CC BY-NC 4.0 legal text
```

### Running Tests

```bash
cd plugins/jarvis/mcp-server
uv run pytest -v
```

### Documentation

- **[CLAUDE.md](CLAUDE.md)** - Development guide and conventions
- **[docs/capabilities.json](docs/capabilities.json)** - Full capability reference (v1.14.0)
- **[docs/](docs/)** - Additional documentation

### Contributing

See [CLAUDE.md](CLAUDE.md) for development conventions:
- Commit message guidelines
- Version bumping workflow
- Plugin reinstall process

**Requirements:**
- Python 3.10+ (for MCP server)
- uv (Python package manager for reproducible builds)

**Contributing:** Issues and PRs welcome!

---

## Architecture Benefits

### Implicit Dependencies

Claude Code doesn't yet support formal plugin dependencies ([GitHub Issue #9444](https://github.com/anthropics/claude-code/issues/9444)), so we use runtime checks:

- Extension agents verify required core tools exist
- Helpful error messages if dependencies missing
- No silent failures

---

## Support

- **Issues:** [GitHub Issues](https://github.com/rsprudencio/claude-plugins/issues)
- **Documentation:** See plugin READMEs and individual SKILL.md files
- **License:** CC BY-NC 4.0 (Attribution-NonCommercial)

---

## License

**CC BY-NC 4.0** (Creative Commons Attribution-NonCommercial 4.0 International)

- ✅ Share and adapt for non-commercial purposes
- ✅ Attribute the creator
- ❌ No commercial use without permission

See [LICENSE](LICENSE) for full legal text.

---

**Status:** Production Ready v1.14.0
**Latest Release:** 2026-02-08
