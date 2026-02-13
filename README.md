# Jarvis - Personal AI Assistant for Claude Code

A plugin marketplace that turns Claude Code into **Jarvis** — a context-aware personal assistant with a knowledge vault, semantic memory, and strategic awareness.

Jarvis manages a folder of Markdown or Org-mode files (your "vault") as a personal knowledge base. It journals your thoughts, tracks your goals, searches by meaning, and learns from your conversations — all with a git-audited trail.

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
claude plugin marketplace add rsprudencio/claude-plugins

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

### Option 3: Docker

Run the MCP server in a container — no Python or ChromaDB compilation needed on your machine.

```bash
# Install the plugin (still needed for skills, agents, system prompt)
claude plugin marketplace add rsprudencio/claude-plugins
claude plugin install jarvis@raph-claude-plugins

# Start the MCP server container
docker pull ghcr.io/rsprudencio/jarvis:latest
docker compose -f ~/.jarvis/docker-compose.yml up -d

# Tell Claude Code to use HTTP transport
claude mcp add --transport http jarvis-core http://localhost:8741/mcp
```

Or use the installer with Docker option: `curl -fsSL ... | bash` and choose **[2] Docker** when prompted.

Switch between local/container/remote modes anytime with `~/.jarvis/jarvis-transport.sh` or `/jarvis-settings` inside Claude Code.

See [docker/README.md](docker/README.md) for full Docker setup guide.

### Option 4: Clone and Install Locally

```bash
git clone https://github.com/rsprudencio/claude-plugins.git
cd claude-plugins
claude plugin marketplace add rsprudencio/claude-plugins
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
claude              # Then type /jarvis:jarvis to activate mid-session
```

### First Things to Try

| Command | What it does |
|---------|--------------|
| `jarvis, journal this: I decided to use PostgreSQL for the new project` | Creates a journal entry with vault links |
| `/jarvis-recall database decisions` | Searches your vault by meaning |
| `/jarvis-orient` | Strategic briefing — what to focus on today |
| `/jarvis-todoist` | Process your Todoist inbox with smart routing |
| `/jarvis-promote` | Review and promote auto-captured observations |

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
| **jarvis-todoist** | Smart task routing, inbox capture, Todoist sync | Optional (needs [API token](https://app.todoist.com/app/settings/integrations/developer)) |
| **jarvis-strategic** | Orient briefings, catch-up, summaries, pattern analysis | Optional |

### 14 Skills (Slash Commands)

**Core:**
| Skill | Description |
|-------|-------------|
| `/jarvis:jarvis` | Activate Jarvis identity mid-session |
| `/jarvis-settings` | View and update configuration |
| `/jarvis-journal` | Create journal entries with intelligent vault linking |
| `/jarvis-inbox` | Process and organize vault inbox items |
| `/jarvis-recall <query>` | Semantic search across your vault |
| `/jarvis-promote` | Browse and promote auto-captured observations |
| `/jarvis-memory-stats` | Memory system health and statistics |
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
Your vault files (`.md` or `.org`) and strategic memories. Git-tracked, visible in Obsidian/Emacs, searchable via `/jarvis-recall`.

**Tier 2 — Ephemeral (Auto-Generated)**
Observations captured from your conversations, patterns, summaries. Lives in ChromaDB only. Review with `/jarvis-promote` — valuable items get promoted to permanent vault files.

**Auto-Extract** runs passively after each conversation turn, using Haiku to identify insights worth remembering. Configurable modes: `background` (recommended), `background-api`, `background-cli`, or `disabled`.

---

## Platform Support

Jarvis works on **macOS, Linux, Windows, and WSL** with automatic platform detection and tailored error messages.

### Supported Platforms

| Platform | Status | Notes |
|----------|--------|-------|
| **macOS** 13+ | ✅ Fully supported | Homebrew, Command Line Tools, or python.org |
| **Linux** (Ubuntu, Debian, RedHat, CentOS) | ✅ Fully supported | apt, yum, or python.org |
| **Windows** 11 | ✅ Supported | Git Bash, PowerShell, or WSL2 |
| **WSL2** | ✅ Fully supported | Native Linux tooling inside Windows |

### Windows-Specific Notes

**Python**: Windows typically uses `python` instead of `python3`. Jarvis automatically detects both.

**Install options:**
- **Microsoft Store**: Search for "Python 3.11" or "Python 3.12" (easiest, adds to PATH automatically)
- **python.org**: Download installer, check "Add Python to PATH" during install
- **WSL2**: Recommended for advanced users, gives you full Linux environment

**Git**: Install [Git for Windows](https://git-scm.com/download/win) which includes Git Bash - a Unix-like terminal for Windows.

**uv**: Download installer from [astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/)

**Shell integration**: The `jarvis` shell function works in Git Bash, PowerShell, and WSL.

### PATH Enrichment

Jarvis automatically checks common install locations even if they're not in your `PATH`:

**Unix (macOS/Linux):**
- `~/.local/bin` — pip user installs, uv
- `~/.cargo/bin` — Rust tools (alternative uv install)

**Windows:**
- `%LOCALAPPDATA%\Programs\Python` — Microsoft Store Python
- `%PROGRAMFILES%\Git\cmd` — Git for Windows

If a tool is installed but not found, Jarvis provides platform-specific installation instructions.

---

## Configuration

All configuration lives in `~/.jarvis/config.json`. The installer writes a full config with all ~30 keys visible so you can discover and tweak options.

Run `/jarvis-settings` anytime to update configuration through a guided menu.

Key config sections:
- **vault_path** — Where your vault files live
- **file_format** — `"md"` (Markdown, default) or `"org"` (Org-mode) for new files
- **memory.auto_extract** — Observation capture mode and thresholds
- **promotion** — When ephemeral content gets promoted to vault files
- **paths** — Vault directory layout (all customizable)

---

## Troubleshooting

### Prerequisites Check Failed

If `install.sh` or `check-requirements` reports missing prerequisites:

**"Python not found"**
- **macOS**: `brew install python@3.12` or download from [python.org](https://python.org)
- **Linux**: `sudo apt install python3` (Debian/Ubuntu) or `sudo yum install python3` (RedHat/CentOS)
- **Windows**: Install from [Microsoft Store](https://www.microsoft.com/store/productId/9NCVDN91XZQP) or [python.org](https://python.org/downloads/)

**"uv not found"**
- **macOS/Linux**: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Windows**: Download from [astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/)
- After install, restart your terminal or run `export PATH="$HOME/.local/bin:$PATH"`

**"git not found"**
- **macOS**: `xcode-select --install` or `brew install git`
- **Linux**: `sudo apt install git` (Debian/Ubuntu) or `sudo yum install git` (RedHat/CentOS)
- **Windows**: Download [Git for Windows](https://git-scm.com/download/win)

**"Python version too old"**
- Jarvis requires Python 3.10 or later
- Check version: `python3 --version` (or `python --version` on Windows)
- Upgrade using your platform's package manager or python.org installer

### Plugin Not Loading

If `/jarvis:jarvis` or `/jarvis-settings` doesn't work:

1. **Verify plugin installed**: `claude plugin list` — should show `jarvis@raph-claude-plugins`
2. **Check MCP server**: Look for "Jarvis MCP server started" in Claude Code logs
3. **Run diagnostics**: `./check-requirements` from repo root
4. **Reinstall**: Use the `/reinstall` skill or manually:
   ```bash
   rm -rf ~/.claude/plugins/cache/raph-claude-plugins/jarvis/*
   claude plugin uninstall jarvis@raph-claude-plugins
   claude plugin install jarvis@raph-claude-plugins
   ```
5. **Full restart**: Quit and reopen Claude Code (not just reload)

### Auto-Extract Not Working

If observations aren't being captured:

1. **Check mode**: Run `/jarvis-settings` → Auto-Extract Configuration
   - `background` mode requires Claude API key
   - `background-cli` mode requires Claude CLI installed (`claude --version`)
2. **Check logs**: Look for "Auto-extract" in Claude Code debug logs
3. **Verify hooks**: `ls ~/.claude/plugins/cache/raph-claude-plugins/jarvis/*/hooks/` should show `Stop.md`

### Memory/Semantic Search Issues

If `/recall` returns no results or indexing fails:

1. **Check database**: `ls ~/.jarvis/memory_db/` — should contain ChromaDB files
2. **Rebuild index**: Run `/memory-index` to re-index all vault files
3. **Check stats**: Run `/memory-stats` to see document count
4. **Verify Python packages**: Jarvis uses `chromadb` via uv — should auto-install on first use

### Windows-Specific Issues

**"Command not found" in Git Bash**
- Tools installed via Microsoft Store may not be in Git Bash's PATH
- Either use PowerShell, or add to Git Bash PATH: `export PATH="/c/Users/YourName/AppData/Local/Microsoft/WindowsApps:$PATH"`

**WSL vs Native Windows**
- If using WSL: Install Linux versions of tools inside WSL (`sudo apt install python3 git`)
- If using native Windows: Install Windows versions of tools

**Permission errors**
- Git Bash may need "Run as Administrator" for some operations
- WSL requires no special permissions

### Still Having Issues?

- **Check logs**: Claude Code → View → Toggle Developer Tools → Console
- **Verbose diagnostics**: `./check-requirements --verbose`
- **Report bug**: [GitHub Issues](https://github.com/rsprudencio/claude-plugins/issues)

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

**v1.23.0** | [Issues](https://github.com/rsprudencio/claude-plugins/issues) | [Changelog](CLAUDE.md#version-history)
