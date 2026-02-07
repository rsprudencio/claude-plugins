---
name: jarvis-setup
description: Interactive setup wizard for Jarvis. Configure vault path and shell integration.
user_invocable: true
---

# /jarvis-setup - Jarvis Setup Wizard

Run this wizard to configure Jarvis. It's idempotent - re-running updates existing config.

## CRITICAL: No Auto-Activation

**DO NOT** automatically:
- Activate any external MCP
- Load any project
- Call any MCP tools before user confirms

**You CAN**:
- Scan filesystem to detect potential vault paths
- Check which tools are available in your tool list
- Read ~/.jarvis/config.json

**ALWAYS ask and get confirmation** before activating MCPs or loading projects.

## Execution Flow

### 1. Load existing config (if any)

```bash
cat ~/.jarvis/config.json 2>/dev/null
```

This is the ONLY file you read automatically.

### 2. Ask vault path

Use AskUserQuestion:

```
AskUserQuestion:
  questions:
    - question: "Vault path? (current: [path or 'not set'])"
      header: "Vault"
      options:
        - label: "Keep current" (only if already set, make this first/default)
        - label: "[detected path 1]" (if found common locations like ~/Documents/Obsidian, etc.)
        - label: "[detected path 2]"
        - label: "Enter custom path..."
      multiSelect: false
```

You can scan common locations (~/Documents/Obsidian, ~/vaults, etc.) to suggest options.

### 3. Write config after answer

Only after receiving the answer, write `~/.jarvis/config.json`:

```bash
mkdir -p ~/.jarvis
```

```json
{
  "vault_path": "[user's answer]",
  "vault_confirmed": true,
  "configured_at": "[ISO 8601 timestamp]",
  "version": "0.2.0"
}
```

**IMPORTANT**: The `vault_confirmed: true` field is required for vault file operations to work. Without it, the MCP tools will refuse to write to the vault. This ensures setup was explicitly run.

### 4. Report available capabilities

List which agents are available based on detected MCPs:

```
Available agents:
- jarvis-audit-agent (git commits) - Always available
- jarvis-journal-agent (vault journals) - Always available
- jarvis-todoist-agent (task sync) - Requires Todoist MCP [✓ detected | ✗ not found]

Note: All agents are always loaded. They will fail gracefully if their required MCP is not configured.
```

If Todoist MCP is missing, suggest:
```
To enable full functionality:
- Todoist: Configure Todoist MCP in settings
```

### 5. Memory system setup

Ensure the memory database directory exists (location is configurable via `memory.db_path` in config, default: `~/.jarvis/memory_db`):

```bash
mkdir -p ~/.jarvis/memory_db
```

Then suggest indexing:

```
Jarvis has semantic search powered by ChromaDB.
DB location: ~/.jarvis/memory_db/ (configurable via memory.db_path in config)
Run /jarvis:jarvis-memory-index to index your vault for /recall searches.
```

### 6. Mention strategic context

```
Jarvis stores strategic context (values, goals, trajectory) in your vault at .jarvis/strategic/.
Run /jarvis:jarvis-interview anytime to set or update these.
```

### 7. Shell Integration (Optional but Recommended)

Ask if the user wants the `jarvis` command added to their shell:

```
AskUserQuestion:
  questions:
    - question: "Add 'jarvis' shell command for quick access?"
      header: "Shell"
      options:
        - label: "Yes, add to my shell config (Recommended)"
          description: "Adds jarvis() function to ~/.zshrc or ~/.bashrc"
        - label: "No, I'll set it up manually"
          description: "You can copy from plugins/jarvis/shell/"
      multiSelect: false
```

If user says yes:

1. **Detect shell** - Check `$SHELL` environment variable
2. **Find plugin's shell snippet**:
   ```bash
   # Get the plugin install path
   local plugin_dir=$(claude plugin list --json 2>/dev/null | jq -r '.[] | select(.id | startswith("jarvis@")) | .installPath')
   ```
3. **Read the appropriate snippet**:
   - For zsh: `$plugin_dir/shell/jarvis.zsh`
   - For bash: `$plugin_dir/shell/jarvis.bash`
4. **Check if already exists** in shell config (grep for "function jarvis")
5. **Append to shell config** (with user confirmation):
   - zsh: `~/.zshrc`
   - bash: `~/.bashrc`
6. **Remind user to reload**: `source ~/.zshrc` or restart terminal

**Shell Snippet Location**: The canonical function lives in `plugins/jarvis/shell/`:
- `jarvis.zsh` - For zsh users (uses zsh glob qualifiers)
- `jarvis.bash` - For bash users (uses find command)

If adapting for other shells, use the zsh version as reference and adjust syntax as needed.

### 8. Suggest permissions (for smoother experience)

Suggest adding these permissions to avoid repeated prompts:

```
For a smoother experience, add to ~/.claude/settings.json:

{
  "permissions": {
    "allow": [
      "Read(~/.jarvis/*)"
    ]
  }
}

This allows Jarvis to read its config without prompting each time.
```

### 9. Show completion summary

```
Setup complete!

Vault: [path]
Shell: [jarvis command added to ~/.zshrc | not configured]

Quick Start:
  $ jarvis                - Launch Claude with Jarvis identity (if shell configured)
  /jarvis                 - Activate Jarvis mode (within Claude)
  /jarvis:jarvis-setup    - Update this config
  /jarvis:jarvis-interview - Set values/goals
```

## Key Rules

- **NO AUTO-LOADING** - Don't call MCP tools before confirmation
- **ASK FIRST** - Always get user confirmation before any action
- **Write config ONCE** at the end
- **All agents always available** - No enable/disable, they fail gracefully if MCP missing
