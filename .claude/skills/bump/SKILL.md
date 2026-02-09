---
description: Bump Jarvis plugin version and stage files for commit
---

# Version Bump Workflow

You are helping the developer bump the Jarvis plugin version.

## Modular Architecture

The plugin is split into 3 independent plugins:

| Plugin | Path | Description |
|--------|------|-------------|
| `jarvis` | `plugins/jarvis/.claude-plugin/plugin.json` | Core (vault, journal, audit) |
| `jarvis-todoist` | `plugins/jarvis-todoist/.claude-plugin/plugin.json` | Todoist integration |
| `jarvis-strategic` | `plugins/jarvis-strategic/.claude-plugin/plugin.json` | Strategic analysis |

**Default:** Bump `jarvis` (core) unless user specifies otherwise.

## Workflow

### Step 1: Determine Which Plugin

If user specifies a plugin name (e.g., `/bump minor todoist`), bump that plugin.
Otherwise, default to `jarvis` (core).

### Step 2: Read Current Version

Read the appropriate `plugins/<plugin>/.claude-plugin/plugin.json` to get the current version.

### Step 3: Determine Bump Type

Based on the user's request or auto-detect from changes:

- **patch** (X.Y.Z → X.Y.Z+1): Bug fixes, docs, minor tweaks, small changes
- **minor** (X.Y.Z → X.Y+1.0): New features, agents, skills, workflow changes
- **major** (X.Y.Z → X+1.0.0): Breaking changes (MUST confirm with user first)

**Default:** patch if user doesn't specify

If user says "major", ask for confirmation:
> Warning: **Major version bump** changes X.Y.Z → X+1.0.0
> This indicates breaking changes. Are you sure? (yes/no)

### Step 4: Calculate New Version

Parse current version and calculate new version based on bump type.

### Step 5: Update Version Files

**File 1: `plugins/<plugin>/.claude-plugin/plugin.json`**
- Update the `version` field to the new version

**File 2: `plugins/<plugin>/mcp-server/pyproject.toml`** (if it exists)
- Update the `version` field to match the plugin version
- This keeps the Python package version in sync with the plugin version
- Critical: `uvx` caches packages by this version — mismatched versions cause stale code

**File 3: `CLAUDE.md`** (only for minor/major bumps to core plugin)
- Find the "### Version History" section
- Add new entry at the top:
  ```markdown
  - **X.Y.Z** - Brief description based on recent changes
  ```

For patch bumps or extension plugins, skip updating CLAUDE.md.

### Step 6: Stage Files

```bash
git add plugins/<plugin>/.claude-plugin/plugin.json
```

If pyproject.toml was updated:
```bash
git add plugins/<plugin>/mcp-server/pyproject.toml
```

If CLAUDE.md was updated:
```bash
git add CLAUDE.md
```

### Step 7: Report

```
Version bumped: 1.0.0 → 1.1.0 (minor) [jarvis]

Files staged:
- plugins/jarvis/.claude-plugin/plugin.json
[- CLAUDE.md]

Next steps:
1. Stage other changed files: git add <files>
2. Review: git diff --staged
3. Commit: git commit -m "Your changes and bump to v1.1.0"
4. Tag: git tag -a v1.1.0 -m "Version 1.1.0: Description"
5. Push: git push && git push --tags
6. Reinstall: /reinstall
7. Restart Claude Code
```

---

## Usage Examples

```bash
/bump                 # Bump core (jarvis), default: patch
/bump patch           # Explicit patch bump to core
/bump minor           # Minor bump to core
/bump minor todoist   # Minor bump to jarvis-todoist
/bump patch strategic # Patch bump to jarvis-strategic
/bump major           # Major bump (will confirm first)
```

---

## Notes

- This skill ONLY bumps version and stages version files
- Does NOT commit, tag, or reinstall (those are separate steps)
- Use `/reinstall` AFTER committing to apply changes to Claude Code
- Version bumps should be part of feature commits (combined), not standalone
- Extension plugins (todoist, strategic) are versioned independently from core
