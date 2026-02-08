# Jarvis - Personal AI Assistant for Claude Code

A plugin marketplace that turns Claude Code into **Jarvis** — a context-aware personal assistant with a knowledge vault, semantic memory, and strategic awareness.

Jarvis manages a folder of markdown files (your "vault") as a personal knowledge base. It journals your thoughts, tracks your goals, searches by meaning, and learns from your conversations — all with a git-audited trail.

---

## Quick Install

### Option 1: One-Line Installer (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/rsprudencio/claude-plugins/refs/heads/master/install.sh | bash
```

This validates prerequisites, installs the plugin, configures your vault, and sets up the `jarvis` shell command — all in one step.

### Option 2: Manual Install

If you prefer to install step by step:

**Prerequisites:**
- [Claude Code CLI](https://claude.ai/code) (latest version)
- [Python 3.10+](https://python.org)
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

**Install:**

```bash
# Add the marketplace
claude plugin marketplace add raph-claude-plugins https://github.com/rsprudencio/claude-plugins

# Install core plugin (required)
claude plugin install jarvis@raph-claude-plugins

# Optional: Todoist integration
claude plugin install jarvis-todoist@raph-claude-plugins

# Optional: Strategic analysis (orient, catch-up, patterns, summaries)
claude plugin install jarvis-strategic@raph-claude-plugins
```

**First-time config:**

```
/jarvis-settings
```

This walks you through vault path selection, auto-extract mode, and shell integration.

### Option 3: Clone and Install Locally

```bash
git clone https://github.com/rsprudencio/claude-plugins.git
cd claude-plugins
claude plugin marketplace add raph-claude-plugins .
claude plugin install jarvis@raph-claude-plugins
```

### Verify Installation

After installing, start Claude Code and run:

```
/jarvis-settings
```

If the MCP server loaded correctly, you'll see the configuration menu. If not, run `check-requirements` from the repo root to diagnose.

---

## Getting Started

Once installed, launch Jarvis:

```bash
jarvis              # If you set up the shell command during install
# or
claude              # Then type /jarvis to activate mid-session
```

### First Things to Try

| Command | What it does |
|---------|--------------|
| `jarvis, journal this: I decided to use PostgreSQL for the new project` | Creates a journal entry with vault links |
| `/recall database decisions` | Searches your vault by meaning |
| `/jarvis-orient` | Strategic briefing — what to focus on today |
| `/jarvis-todoist` | Process your Todoist inbox with smart routing |
| `/promote` | Review and promote auto-captured observations |

### How Jarvis Works

1. **You talk to Claude as usual** — Jarvis runs in the background
2. **Auto-extract** captures valuable insights from your conversations into ephemeral memory
3. **Journal entries** create permanent, linked notes in your vault
4. **Semantic search** finds related content by meaning, not just keywords
5. **Strategic context** keeps you aligned with your goals across sessions

---

## What's in the Box

### 3 Composable Plugins

| Plugin | What | Install |
|--------|------|---------|
| **jarvis** (core) | Vault management, journals, git audit, semantic memory, auto-extract | Required |
| **jarvis-todoist** | Smart task routing, inbox capture, Todoist sync | Optional (needs [Todoist MCP](https://todoist.com)) |
| **jarvis-strategic** | Orient briefings, catch-up, summaries, pattern analysis | Optional |

### 14 Skills (Slash Commands)

**Core:**
| Skill | Description |
|-------|-------------|
| `/jarvis` | Activate Jarvis identity mid-session |
| `/jarvis-settings` | View and update configuration |
| `/jarvis-journal` | Create journal entries with intelligent vault linking |
| `/jarvis-inbox` | Process and organize vault inbox items |
| `/recall <query>` | Semantic search across your vault |
| `/promote` | Browse and promote auto-captured observations |
| `/memory-stats` | Memory system health and statistics |
| `/jarvis-schedule` | Manage recurring Jarvis actions |
| `/jarvis-audit` | Git audit protocol reference |

**Todoist** (requires jarvis-todoist plugin):
| Skill | Description |
|-------|-------------|
| `/jarvis-todoist` | Sync Todoist inbox with smart routing |
| `/jarvis-todoist-setup` | Configure Todoist routing rules |

**Strategic** (requires jarvis-strategic plugin):
| Skill | Description |
|-------|-------------|
| `/jarvis-orient` | Strategic briefing for starting a session |
| `/jarvis-catchup` | Catch-up after time away |
| `/jarvis-summarize` | Weekly/monthly journal summaries |
| `/jarvis-patterns` | Behavioral pattern analysis |

### 3 Agents (Autonomous Sub-Processes)

Jarvis delegates work to specialized agents that run in isolated context windows:

- **Journal Agent** (Haiku) — Drafts entries with vault linking and YAML frontmatter
- **Audit Agent** (Haiku) — JARVIS protocol git commits, history queries, rollbacks
- **Explorer Agent** (Haiku) — Vault-wide search with regex, date filtering, semantic pre-search

### 21 MCP Tools

Python-based MCP server providing vault filesystem, git operations, semantic memory, content API, and path configuration. See [docs/capabilities.json](docs/capabilities.json) for the full reference.

---

## Memory System

Jarvis has a two-tier semantic memory powered by ChromaDB:

**Tier 1 — File-Backed (Permanent)**
Your vault markdown files and strategic memories. Git-tracked, visible in Obsidian, searchable via `/recall`.

**Tier 2 — Ephemeral (Auto-Generated)**
Observations captured from your conversations, patterns, summaries. Lives in ChromaDB only. Review with `/promote` — valuable items get promoted to permanent vault files.

**Auto-Extract** runs passively after each conversation turn, using Haiku to identify insights worth remembering. Configurable modes: `background` (recommended), `background-api`, `background-cli`, or `disabled`.

---

## Configuration

All configuration lives in `~/.jarvis/config.json`. The installer writes a full config with all ~30 keys visible so you can discover and tweak options.

Run `/jarvis-settings` anytime to update configuration through a guided menu.

Key config sections:
- **vault_path** — Where your markdown files live
- **memory.auto_extract** — Observation capture mode and thresholds
- **promotion** — When ephemeral content gets promoted to vault files
- **paths** — Vault directory layout (all customizable)

---

## Development

### Repository Structure

```
.
├── install.sh                    # Curl-pipe-bash installer
├── check-requirements            # Standalone prereq checker
├── plugins/
│   ├── jarvis/                   # Core plugin
│   │   ├── agents/               # Journal, audit, explorer agents
│   │   ├── skills/               # 9 core skills
│   │   ├── mcp-server/           # Python MCP server (21 tools, 652 tests)
│   │   ├── hooks/                # Auto-extract Stop hook
│   │   └── system-prompt.md      # Jarvis identity
│   ├── jarvis-todoist/           # Todoist extension
│   └── jarvis-strategic/         # Strategic analysis extension
├── CLAUDE.md                     # Development guide
└── LICENSE                       # CC BY-NC 4.0
```

### Running Tests

```bash
cd plugins/jarvis/mcp-server && uv run pytest -v
```

652 unit tests covering config, file ops, git operations, memory, protocol, and server registration.

### Documentation

- **[CLAUDE.md](CLAUDE.md)** — Development conventions and version history
- **[docs/capabilities.json](docs/capabilities.json)** — Full capability reference

---

## License

**CC BY-NC 4.0** (Creative Commons Attribution-NonCommercial 4.0 International)

See [LICENSE](LICENSE) for full legal text.

---

**v1.15.0** | [Issues](https://github.com/rsprudencio/claude-plugins/issues) | [Changelog](CLAUDE.md#version-history)
