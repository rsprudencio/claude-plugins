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
PostgreSQL:   postgresql://jarvis:***@localhost:5432/jarvis (ok, 2657 docs)
Embedding:    granite-small-r2 (384d, onnx)
Auto-Extract: background (min text: 200 chars)
Replication:  disabled
Shell:        jarvis command in PATH
Version:      2.3.0
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
        - label: "PostgreSQL connection"
          description: "Configure database URL, test connectivity"
        - label: "Embedding model"
          description: "View/change embedding model and dimensions"
        - label: "File format"
          description: "Choose Markdown (.md) or Org-mode (.org) for new files"
        - label: "Auto-Extract"
          description: "Configure observation capture from conversations"
        - label: "Replication"
          description: "Configure bidirectional sync between instances"
        - label: "Statusline"
          description: "Install or update the Claude Code statusline"
        - label: "Advanced settings"
          description: "Vault paths, memory tuning, context enrichment"
      multiSelect: false
```

### 2a. First-time flow (no config exists)

If no config exists, skip the menu and walk through essentials:

1. **Vault path** (Step 3a)
2. **File format** (Step 3b) - Markdown or Org-mode
3. **Auto-Extract mode** (Step 3c) - quick preset selection only
4. **Shell integration** (Step 3i below)
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

### 3d. PostgreSQL connection

Show current connection (mask password) and offer test:

```
AskUserQuestion:
  questions:
    - question: "PostgreSQL configuration?"
      header: "PostgreSQL"
      options:
        - label: "Test connection"
          description: "Verify connectivity and show doc count"
        - label: "Change connection URL"
          description: "Set postgres_url (current: ***@localhost:5432/jarvis)"
        - label: "Back to main menu"
      multiSelect: false
```

**Test connection**: Call `/health` endpoint or use `jarvis_collection_stats` tool to verify PG is reachable. Display: status, doc count, table size.

**Change URL**: Ask for new `postgres_url`. Update `memory.postgres_url` in config. Warn: "Changing the database URL means connecting to a different database. Existing data will not be migrated."

Config key: `memory.postgres_url`
Env override: `POSTGRES_URL`

### 3e. Embedding model

Show current model and dimensions:

```
AskUserQuestion:
  questions:
    - question: "Embedding model configuration? (current: granite-small-r2, 384d)"
      header: "Embedding"
      options:
        - label: "View current"
          description: "Show model name, dimensions, device, and backend"
        - label: "Change model"
          description: "⚠️ Requires re-embedding all content"
        - label: "Back to main menu"
      multiSelect: false
```

**View current**: Read from `get_embedding_config()` and display:
- Model: `ibm-granite/granite-embedding-small-english-r2`
- Dimensions: 384
- Device: cpu
- Backend: onnx

**Change model**: Warn prominently:
> ⚠️ Changing the embedding model requires re-embedding ALL stored content.
> The stored model in jarvis_meta must match the active config.
> After changing, run: `jarvis-init-db --force-model-record`

Config keys: `memory.embedding_model`, `memory.embedding_dimensions`, `memory.embedding_device`, `memory.embedding_backend`
Env overrides: `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `EMBEDDING_DEVICE`, `EMBEDDING_BACKEND`

### 3f. Replication

Show current replication mode:

```
AskUserQuestion:
  questions:
    - question: "Replication configuration? (current: disabled)"
      header: "Replication"
      options:
        - label: "View status"
          description: "Show publications, subscriptions, and sync state"
        - label: "Enable as local node"
          description: "Subscribe to a central Jarvis instance"
        - label: "Enable as central hub"
          description: "Accept subscriptions from local nodes"
        - label: "Disable"
          description: "Turn off replication"
        - label: "Back to main menu"
      multiSelect: false
```

**View status**: Call `/health` endpoint and display replication section (publications, subscriptions, sync lag).

**Enable as local**: Ask for `central_url`, `node_id`. Update config keys:
- `memory.replication.mode` -> `"local"`
- `memory.replication.central_url` -> user input
- `memory.replication.node_id` -> user input

Then instruct: "Run `jarvis-setup-replication --role local --central-url <URL>` to create the PG subscription."

**Enable as central**: Set `memory.replication.mode` -> `"central"`. Instruct: "Run `jarvis-setup-replication --role central` to create the PG publication and replication user."

Config key: `memory.replication`
Env overrides: `JARVIS_REPLICATION_MODE`, `JARVIS_CENTRAL_URL`

### 3g. Advanced settings

```
AskUserQuestion:
  questions:
    - question: "Which advanced area?"
      header: "Advanced"
      options:
        - label: "Vault directory paths"
          description: "Where journals, notes, inbox, etc. are stored"
        - label: "Memory system"
          description: "Secret detection, importance scoring, DB location"
        - label: "Context enrichment"
          description: "Automatic vault memory injection on every message"
        - label: "Back to main menu"
      multiSelect: false
```

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

Let user change specific paths. All paths are relative to vault root.

#### Memory system

| Setting | Default | Description |
|---------|---------|-------------|
| `secret_detection` | true | Scan content for secrets before storing |
| `importance_scoring` | true | Score content importance on write |
| `recency_boost_days` | 7 | Boost recent content in search results |
| `default_importance` | 0.5 | Default importance score (0.0–1.0) for new content |
| `postgres_url` | `postgresql://jarvis:jarvis@localhost:5432/jarvis` | PostgreSQL connection URL |
| `embedding_model` | `ibm-granite/granite-embedding-small-english-r2` | Sentence-transformer model |
| `embedding_dimensions` | `384` | Vector dimensions |
| `embedding_device` | `cpu` | Compute device (cpu/cuda) |
| `embedding_backend` | `onnx` | Inference backend (onnx/pytorch) |

#### Cross-encoder reranking

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | false | Enable cross-encoder reranking for `/jarvis-recall` results (disabled by default: ms-marco-MiniLM measured net-negative, −0.055 nDCG@10) |
| `candidate_count` | 100 | How many results to over-fetch for reranking |
| `top_k` | 10 | Legacy reranking cap; the caller's `n_results` decides the final result count |
| `alpha` | 0.7 | Blend weight (0.0=vector only, 1.0=reranker only) |
| `max_latency_ms` | 1000 | Latency budget before fallback to vector scores |
| `batch_size` | 32 | Tokenization batch size for ONNX inference |

Config key: `memory.reranking`

Note: The ONNX model (~23MB) is downloaded automatically on first use to `~/.jarvis/models/cross-encoder/`. Not applied to context enrichment.

#### Context enrichment

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | true | Master switch for automatic memory recall |
| `threshold` | 0.876 | Minimum raw cosine similarity; higher values favor precision |
| `budget` | 8000 | Total character budget for injection (split 50/50 between auto-generated content and vault documents) |
| `max_results` | 20 | Maximum injected matches after thresholding, ranking, and deduplication |

Offer presets:
- "Calibrated high precision" (default) -> `threshold: 0.876, budget: 8000, max_results: 20`
- "Aggressive recall" -> `threshold: 0.80, budget: 12000, max_results: 20`
- "Conservative" -> `threshold: 0.88, budget: 4000, max_results: 10`
- "Disabled" -> `enabled: false`
- "Custom" -> Ask for each setting individually

Config key: `memory.context_enrichment`

### 3h. View full config

Pretty-print `~/.jarvis/config.json` grouped by section:

```
=== Core ===
vault_path:      ~/.jarvis/vault/
file_format:     md
vault_confirmed: true
configured_at:   2026-02-08T10:30:00Z

=== Memory ===
postgres_url:         postgresql://jarvis:***@localhost:5432/jarvis
embedding_model:      ibm-granite/granite-embedding-small-english-r2
embedding_dimensions: 384
secret_detection:     true
importance_scoring:   true
recency_boost_days:   7
default_importance:   0.5

=== Context Enrichment ===
enabled:              true
threshold:            0.876
budget:               8000
max_results:          20

=== Auto-Extract ===
mode:                 background
min_turn_chars:       200
max_transcript_lines: 500
debug:                false

=== Replication ===
mode:                 disabled
central_url:          (not set)
node_id:              (not set)

=== Vault Paths (relative to vault root) ===
journal_jarvis:         journal/jarvis
journal_daily:          journal/daily
notes:                  notes
...
```

Highlight any non-default values with a marker. After viewing, return to main menu.

### 3i. Install jarvis command

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

### 3j. Statusline

Install or update the Jarvis statusline for Claude Code. The statusline shows model, MCP servers, cost, duration, context usage, and Jarvis server health.

**Check current state:**
1. Look for `statusLine` in the user's `settings.json` (at `$CLAUDE_CONFIG_DIR/settings.json` or `~/.claude/settings.json`)
2. Check if `~/.jarvis/statusline.py` exists

**If not installed:**
1. Locate `statusline.py` in the plugin's `statusline/` directory (use the skill's base directory, two levels up from `skills/<name>/`)
2. Copy to `~/.jarvis/statusline.py` and `chmod +x`
3. Merge `statusLine` config into the user's `settings.json`:
   ```json
   {"statusLine": {"type": "command", "command": "~/.jarvis/statusline.py"}}
   ```
4. Tell user: "Restart Claude Code for the statusline to appear."

**If already installed:**
Offer to update from the plugin distribution (in case a new version shipped).

**Important:** The user's `settings.json` path depends on their `CLAUDE_CONFIG_DIR`. If the user runs multiple Claude configs, ask which settings.json to update.

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
mkdir -p ~/.jarvis/db
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
