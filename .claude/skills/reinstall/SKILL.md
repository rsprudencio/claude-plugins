---
description: Reinstall Jarvis plugin with cache clear
---

# Jarvis Plugin Reinstall Workflow

You are helping the developer reinstall the Jarvis plugin during active development.

## Workflow

### Step 1: Clear Cache & Reinstall

Run the following command chain:

```bash
rm -rf ~/.claude/plugins/cache/jarvis-marketplace/jarvis/* && \
claude plugin marketplace update && \
claude plugin uninstall jarvis@jarvis-marketplace && \
claude plugin install jarvis@jarvis-marketplace
```

### Step 2: Remind User to Restart

**IMPORTANT:** Plugin changes only take effect after a full restart of Claude Code, not just a reload.

Inform the user:
```
✅ Plugin reinstalled successfully

⚠️  RESTART REQUIRED
Plugin changes will only take effect after you restart Claude Code.

- macOS: Cmd+Q to quit, then reopen
- Linux/Windows: Close and reopen the application
```

---

## Usage

```
/reinstall
```

---

## Notes

- This skill is for development only (not included in the plugin distribution)
- Never skip the restart reminder - users often forget this step
