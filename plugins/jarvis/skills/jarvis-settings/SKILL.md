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
Auto-Extract: background (cooldown: 120s, min text: 200 chars)
Memory DB:    ~/.jarvis/memory_db/ (configurable)
Shell:        jarvis command in ~/.zshrc
Version:      1.15.0
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
        - label: "Auto-Extract"
          description: "Configure observation capture from conversations"
        - label: "Advanced settings"
          description: "Promotion thresholds, vault paths, memory tuning"
        - label: "View full config"
          description: "Show all settings with current values"
      multiSelect: false
```

### 2a. First-time flow (no config exists)

If no config exists, skip the menu and walk through essentials:

1. **Vault path** (Step 3a)
2. **Auto-Extract mode** (Step 3b) - quick preset selection only
3. **Shell integration** (Step 3e below)
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

### 3b. Auto-Extract

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
          description: "Cooldown: 120s, Min text: 200 chars"
        - label: "Frequent capture"
          description: "Cooldown: 30s, Min text: 100 chars (more observations, slightly noisier)"
        - label: "Conservative"
          description: "Cooldown: 300s, Min text: 500 chars (fewer, higher quality)"
        - label: "Custom values"
          description: "Enter your own thresholds"
      multiSelect: false
```

Preset mapping:
- "Defaults" -> `cooldown_seconds: 120, min_turn_chars: 200`
- "Frequent capture" -> `cooldown_seconds: 30, min_turn_chars: 100`
- "Conservative" -> `cooldown_seconds: 300, min_turn_chars: 500`
- "Custom" -> Ask for `cooldown_seconds` (int) and `min_turn_chars` (int)

### 3c. Advanced settings

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
| `observations_promoted` | `journal/jarvis/observations` | ... |
| `patterns_promoted` | `journal/jarvis/patterns` | ... |
| `learnings_promoted` | `journal/jarvis/learnings` | ... |
| `decisions_promoted` | `journal/jarvis/decisions` | ... |

Let user change specific paths. All paths are relative to vault root.

#### Memory system

| Setting | Default | Description |
|---------|---------|-------------|
| `secret_detection` | true | Scan content for secrets before storing |
| `importance_scoring` | true | Score content importance on write |
| `recency_boost_days` | 7 | Boost recent content in search results |
| `default_importance` | "medium" | Default importance for new content |
| `db_path` | `~/.jarvis/memory_db` | ChromaDB database location |

### 3d. View full config

Pretty-print `~/.jarvis/config.json` grouped by section:

```
=== Core ===
vault_path:      ~/.jarvis/vault/
vault_confirmed: true
configured_at:   2026-02-08T10:30:00Z

=== Memory ===
db_path:              ~/.jarvis/memory_db
secret_detection:     true
importance_scoring:   true
recency_boost_days:   7
default_importance:   medium

=== Auto-Extract ===
mode:                 background
min_turn_chars:       200
cooldown_seconds:     120
max_transcript_lines: 100
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

### 3e. Shell integration

If user chose this from advanced, or during first-time flow:

```
AskUserQuestion:
  questions:
    - question: "Add 'jarvis' shell command for quick access?"
      header: "Shell"
      options:
        - label: "Yes, add to my shell config (Recommended)"
          description: "Adds jarvis() function to ~/.zshrc or ~/.bashrc"
        - label: "No, skip"
          description: "You can set it up later with /jarvis-settings"
      multiSelect: false
```

If yes:
1. Detect shell from `$SHELL`
2. Check if already exists (grep for "function jarvis")
3. Append snippet to shell config
4. Remind to `source ~/.zshrc`

### 4. Write config

**Merge** changes into existing config. Never overwrite keys that weren't changed.

For first-time setup, write the FULL config with all defaults visible:

```json
{
  "vault_path": "[user's choice]",
  "vault_confirmed": true,
  "configured_at": "[ISO 8601]",
  "memory": {
    "db_path": "~/.jarvis/memory_db",
    "secret_detection": true,
    "importance_scoring": true,
    "recency_boost_days": 7,
    "default_importance": "medium",
    "auto_extract": {
      "mode": "[selected]",
      "min_turn_chars": 200,
      "cooldown_seconds": 120,
      "max_transcript_lines": 100,
      "debug": false
    }
  },
  "promotion": {
    "importance_threshold": 0.85,
    "retrieval_count_threshold": 3,
    "age_importance_days": 30,
    "age_importance_score": 0.7,
    "on_promoted_file_deleted": "remove"
  },
  "paths": {
    "journal_jarvis": "journal/jarvis",
    "journal_daily": "journal/daily",
    "notes": "notes",
    "work": "work",
    "inbox": "inbox",
    "inbox_todoist": "inbox/todoist",
    "templates": "templates",
    "strategic": ".jarvis/strategic",
    "observations_promoted": "journal/jarvis/observations",
    "patterns_promoted": "journal/jarvis/patterns",
    "learnings_promoted": "journal/jarvis/learnings",
    "decisions_promoted": "journal/jarvis/decisions"
  }
}
```

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
