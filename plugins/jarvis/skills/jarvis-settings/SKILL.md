---
name: jarvis-settings
description: View and update Jarvis configuration. Use when user says "/jarvis-settings", "Jarvis settings", "update config", "change vault path", or "configure auto-extract".
user_invocable: true
---

# /jarvis-settings - Jarvis Configuration

Re-runnable configuration manager for Jarvis. Jump to any setting, change what you need, leave the rest.

**First-time setup?** If `~/.jarvis/config.json` doesn't exist, guide the user through initial config (vault path + basics), then offer to explore advanced settings.

## Execution Flow

### 1. Load and display current config

Read `~/.jarvis/config.json` and show a summary:

```
Jarvis Configuration
--------------------
Vault:        ~/.jarvis/vault/
File Format:  Markdown (.md)
Auto-Extract: background (min text: 200 chars)
Memory DB:    ~/.jarvis/memory_db/ (configurable)
Shell:        jarvis command in PATH
Version:      1.19.0
```

If config doesn't exist, say: "No config found. Let's set up the basics." and go to Step 2a (first-time flow).

### 2. Ask what to configure

Use AskUserQuestion:

```
AskUserQuestion:
  questions:
    - question: "What would you like to configure?"
      header: "Settings"
      options:
        - label: "Vault path"
          description: "Change where Jarvis stores your knowledge"
        - label: "File format"
          description: "Choose Markdown (.md) or Org-mode (.org) for new files"
        - label: "Auto-Extract"
          description: "Configure observation capture from conversations"
        - label: "MCP Transport"
          description: "Switch between local, Docker container, or remote server"
        - label: "Advanced settings"
          description: "Promotion thresholds, vault paths, memory tuning"
      multiSelect: false
```

### 2a. First-time flow (no config exists)

If no config exists, skip the menu and walk through essentials:

1. **Vault path** (Step 3a)
2. **File format** (Step 3b) - Markdown or Org-mode
3. **Auto-Extract mode** (Step 3c) - quick preset selection only
4. **Shell integration** (Step 3f below)
4. Write full config with ALL defaults visible
5. Offer: "Want to explore advanced settings? Or you're good to go."

### 3a. Vault path

Scan for common vault locations, then ask:

```
AskUserQuestion:
  questions:
    - question: "Where should Jarvis store your knowledge vault?"
      header: "Vault"
      options:
        - label: "~/.jarvis/vault/ (Recommended)"
          description: "Starter vault - good for trying Jarvis or if you don't have a PKM"
        - label: "[detected Obsidian vault]"
          description: "Use your existing Obsidian vault at [path]"
        - label: "Enter custom path"
          description: "Specify your own directory"
      multiSelect: false
```

Scan these locations for Obsidian vaults (contain `.obsidian/`):
- `~/Documents/Obsidian*`
- `~/Obsidian*`
- `~/vaults/*`
- `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/*`

**What is a vault?** If the user seems confused, explain briefly:
> A vault is just a folder of markdown files. If you use Obsidian, it's your Obsidian vault. If not, Jarvis creates a simple folder structure for journals, notes, and strategic context.

After changing vault path: ask if user wants to reindex for semantic search.

### 3b. File format

Show current format (`file_format` in config, default: `md`), then ask:

```
AskUserQuestion:
  questions:
    - question: "File format for new vault files? (current: [format])"
      header: "Format"
      options:
        - label: "Markdown (.md) (Recommended)"
          description: "Standard format, works with Obsidian and most tools"
        - label: "Org-mode (.org)"
          description: "For Emacs/Org-mode users. Existing .md files remain readable."
      multiSelect: false
```

Map to config values:
- "Markdown" -> `"md"`
- "Org-mode" -> `"org"`

Update `file_format` in config. Note: this only affects **new** files. Existing files in either format are always readable and searchable.

### 3c. Auto-Extract

Show current mode, then ask:

```
AskUserQuestion:
  questions:
    - question: "Auto-Extract mode? (current: [mode])"
      header: "Auto-Extract"
      options:
        - label: "Background (Recommended)"
          description: "Smart fallback: tries API first, then CLI. Near-zero cost (~$0.02/session)."
        - label: "Background API only"
          description: "Requires ANTHROPIC_API_KEY. Fastest extraction (~200ms)."
        - label: "Background CLI only"
          description: "Uses Claude CLI via OAuth. No API key needed (~2-5s)."
        - label: "Disabled"
          description: "No automatic observation capture."
      multiSelect: false
```

Map to mode values:
- "Background" -> `"background"`
- "Background API only" -> `"background-api"`
- "Background CLI only" -> `"background-cli"`
- "Disabled" -> `"disabled"`

If NOT disabled, ask about thresholds:

```
AskUserQuestion:
  questions:
    - question: "Extraction sensitivity?"
      header: "Tuning"
      options:
        - label: "Defaults (Recommended)"
          description: "Min text: 200 chars per turn"
        - label: "Frequent capture"
          description: "Min text: 100 chars (more observations, slightly noisier)"
        - label: "Conservative"
          description: "Min text: 500 chars (fewer, higher quality)"
        - label: "Custom values"
          description: "Enter your own min_turn_chars threshold"
      multiSelect: false
```

Preset mapping:
- "Defaults" -> `min_turn_chars: 200`
- "Frequent capture" -> `min_turn_chars: 100`
- "Conservative" -> `min_turn_chars: 500`
- "Custom" -> Ask for `min_turn_chars` (int)

### 3g. MCP Transport

Show current transport mode (`mcp_transport` in config, default: `local`), then ask:

```
AskUserQuestion:
  questions:
    - question: "MCP transport mode? (current: [mode])"
      header: "Transport"
      options:
        - label: "Local (Recommended)"
          description: "stdio via uvx — fastest, no Docker needed"
        - label: "Container"
          description: "Docker on localhost — ports 8741/8742"
        - label: "Remote"
          description: "Docker on another machine — specify URL"
      multiSelect: false
```

Map to config values:
- "Local" -> `"local"`
- "Container" -> `"container"`
- "Remote" -> `"remote"` (then ask for URL)

If "Remote" selected, ask for URL:
```
AskUserQuestion:
  questions:
    - question: "Remote server URL? (e.g., http://192.168.1.50)"
      header: "Remote URL"
      options:
        - label: "Enter URL"
          description: "Base URL of the machine running Docker (no port, no /mcp)"
      multiSelect: false
```

After selecting mode, invoke the transport helper script via Bash:
- For **local**: `bash ~/.jarvis/jarvis-transport.sh local`
- For **container**: `bash ~/.jarvis/jarvis-transport.sh container`
- For **remote**: `bash ~/.jarvis/jarvis-transport.sh remote <url>`

The script handles config updates AND `.mcp.json` rewrites in the plugin cache.
Print: "Transport changed to [mode]. **Restart Claude Code to apply.**"

### 3d. Advanced settings

```
AskUserQuestion:
  questions:
    - question: "Which advanced area?"
      header: "Advanced"
      options:
        - label: "Promotion thresholds"
          description: "When tier 2 content gets promoted to vault files"
        - label: "Vault directory paths"
          description: "Where journals, notes, inbox, etc. are stored"
        - label: "Memory system"
          description: "Secret detection, importance scoring, DB location"
        - label: "Per-prompt search"
          description: "Automatic vault memory injection on every message"
        - label: "Back to main menu"
      multiSelect: false
```

#### Promotion thresholds

Show current values and let user adjust:

| Setting | Default | Description |
|---------|---------|-------------|
| `importance_threshold` | 0.85 | Auto-promote score threshold |
| `retrieval_count_threshold` | 3 | Promote after N retrievals |
| `age_importance_days` | 30 | Age-based promotion trigger |
| `age_importance_score` | 0.7 | Importance threshold for aged content |
| `on_promoted_file_deleted` | "remove" | What happens when promoted file is deleted |

Always offer "Reset to defaults" as an option.

#### Vault directory paths

Show all vault-relative paths with current vs default:

| Path Name | Default | Current |
|-----------|---------|---------|
| `journal_jarvis` | `journal/jarvis` | [current or default] |
| `journal_daily` | `journal/daily` | ... |
| `notes` | `notes` | ... |
| `work` | `work` | ... |
| `inbox` | `inbox` | ... |
| `inbox_todoist` | `inbox/todoist` | ... |
| `templates` | `templates` | ... |
| `strategic` | `.jarvis/strategic` | ... |
| `observations_promoted` | `.jarvis/memories/observations` | ... |
| `patterns_promoted` | `.jarvis/memories/patterns` | ... |
| `learnings_promoted` | `.jarvis/memories/learnings` | ... |
| `decisions_promoted` | `.jarvis/memories/decisions` | ... |

Let user change specific paths. All paths are relative to vault root.

#### Memory system

| Setting | Default | Description |
|---------|---------|-------------|
| `secret_detection` | true | Scan content for secrets before storing |
| `importance_scoring` | true | Score content importance on write |
| `recency_boost_days` | 7 | Boost recent content in search results |
| `default_importance` | "medium" | Default importance for new content |
| `db_path` | `~/.jarvis/memory_db` | ChromaDB database location |

#### Per-prompt search

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | true | Master switch for automatic memory recall |
| `threshold` | 0.5 | Minimum relevance score (0.3=aggressive, 0.5=balanced, 0.7=conservative) |
| `max_results` | 5 | Maximum memories injected per message (1-10) |
| `max_content_length` | 500 | Character limit per memory preview (100-2000) |

Offer presets:
- "Balanced" (default) -> `threshold: 0.5, max_results: 5, max_content_length: 500`
- "Aggressive recall" -> `threshold: 0.4, max_results: 7, max_content_length: 700`
- "Conservative" -> `threshold: 0.6, max_results: 3, max_content_length: 300`
- "Disabled" -> `enabled: false`
- "Custom" -> Ask for each setting individually

Config key: `memory.per_prompt_search`

### 3e. View full config

Pretty-print `~/.jarvis/config.json` grouped by section:

```
=== Core ===
vault_path:      ~/.jarvis/vault/
file_format:     md
vault_confirmed: true
configured_at:   2026-02-08T10:30:00Z

=== MCP Transport ===
mcp_transport:   local
mcp_remote_url:  (not set)

=== Memory ===
db_path:              ~/.jarvis/memory_db
secret_detection:     true
importance_scoring:   true
recency_boost_days:   7
default_importance:   medium

=== Per-Prompt Search ===
enabled:              true
threshold:            0.5
max_results:          5
max_content_length:   500

=== Auto-Extract ===
mode:                 background
min_turn_chars:       200
max_transcript_lines: 500
debug:                false

=== Promotion ===
importance_threshold:        0.85
retrieval_count_threshold:   3
age_importance_days:         30
age_importance_score:        0.7
on_promoted_file_deleted:    remove

=== Vault Paths (relative to vault root) ===
journal_jarvis:         journal/jarvis
journal_daily:          journal/daily
notes:                  notes
...
```

Highlight any non-default values with a marker. After viewing, return to main menu.

### 3f. Install jarvis command

Check if the `jarvis` executable is already installed and in PATH (`command -v jarvis`).

If not installed or outdated, find the shell script in the plugin distribution and install it:

1. Locate `jarvis.sh` in the plugin's `shell/` directory (use the skill's base directory, two levels up from `skills/<name>/`)
2. Determine install directory:
   - `~/.local/bin/` if it exists and is in PATH (preferred)
   - `/usr/local/bin/` if writable
   - Fallback: `~/.local/bin/` (create it, warn about PATH)
3. Copy to `<install_dir>/jarvis` and `chmod +x`
4. If directory not in PATH, tell user to add it: `export PATH="~/.local/bin:$PATH"`

If old shell function markers exist in RC files (`# Jarvis AI Assistant START` in `~/.zshrc`, `~/.bashrc`, or `~/.bash_profile`), offer to clean them up by removing the START/END block.

### 4. Write config

**Merge** changes into existing config. Never overwrite keys that weren't changed.

For first-time setup, read the default config template shipped with the plugin:

1. Locate template: `<plugin_root>/defaults/config.json`
   - Use the skill's base directory (two levels up from `skills/<name>/`) to find `defaults/`
   - Or read it via: `Read` tool on the `defaults/config.json` file relative to the plugin root
2. Read template, substitute user values:
   - `vault_path` -> user's chosen path
   - `vault_confirmed` -> `true`
   - `configured_at` -> current ISO 8601 timestamp
   - `file_format` -> user's chosen format (`"md"` or `"org"`)
   - `auto_extract.mode` -> user's chosen mode
3. Write to `~/.jarvis/config.json`

For existing config updates, **merge** changes — don't overwrite keys that weren't changed.

Also ensure directories exist:
```bash
mkdir -p [vault_path]
mkdir -p ~/.jarvis/memory_db
```

### 5. Summary

Show what changed:

```
Settings updated!

Changed:
  - vault_path: ~/old/path -> ~/new/path
  - auto_extract.mode: disabled -> background

Quick Start:
  /jarvis-recall <query>  - Search vault semantically
  /jarvis-promote         - Review & promote observations
  /jarvis-settings    - Update configuration anytime
  $ jarvis            - Launch from terminal (if shell configured)
```

## Key Rules

- **Menu-driven** - User jumps to any section, not forced through a linear flow
- **Merge, don't overwrite** - Preserve existing config keys when updating a section
- **Show defaults** - First-time config includes ALL keys so users can discover options
- **No broken references** - Don't mention skills or features that don't exist
- **Reindex offer** - When vault path changes, offer to reindex for semantic search
- **Always offer "back"** - User can return to main menu from any sub-section
