---
description: Bump Jarvis plugin version and stage files for commit
---

# Version Bump Workflow

You are helping the developer bump the Jarvis plugin version.

## Workflow

### Step 1: Read Current Version

Read `plugin/.claude-plugin/plugin.json` to get the current version.

### Step 2: Determine Bump Type

Based on the user's request or auto-detect from changes:

- **patch** (X.Y.Z → X.Y.Z+1): Bug fixes, docs, minor tweaks, small changes
- **minor** (X.Y.Z → X.Y+1.0): New features, agents, skills, workflow changes
- **major** (X.Y.Z → X+1.0.0): Breaking changes (MUST confirm with user first)

**Default:** patch if user doesn't specify

If user says "major", ask for confirmation:
> ⚠️ **Major version bump** changes X.Y.Z → X+1.0.0
> This indicates breaking changes. Are you sure? (yes/no)

### Step 3: Calculate New Version

Parse current version and calculate new version based on bump type.

### Step 4: Update Version Files

**File 1: `plugin/.claude-plugin/plugin.json`**
- Update the `version` field to the new version

**File 2: `CLAUDE.md`** (only for minor/major bumps)
- Find the "### Current Version" section or "Version History" section
- Add new entry at the top:
  ```markdown
  - **X.Y.Z** - Brief description based on recent changes
  ```

For patch bumps, skip updating CLAUDE.md (unless significant).

### Step 5: Stage Files

```bash
git add plugin/.claude-plugin/plugin.json
```

If CLAUDE.md was updated:
```bash
git add CLAUDE.md
```

### Step 6: Report

```
✅ Version bumped: 0.3.1 → 0.3.2 (patch)

Files staged:
- plugin/.claude-plugin/plugin.json
[- CLAUDE.md]

Next steps:
1. Stage other changed files: git add <files>
2. Review: git diff --staged
3. Commit: git commit -m "Your changes and bump to v0.3.2"
4. Tag: git tag -a v0.3.2 -m "Version 0.3.2: Description"
5. Push: git push && git push --tags
6. Reinstall: /reinstall
7. Restart Claude Code
```

---

## Usage Examples

```bash
/bump           # Auto-detect (default: patch)
/bump patch     # Explicit patch bump
/bump minor     # Minor version bump
/bump major     # Major version bump (will confirm)
```

---

## Notes

- This skill ONLY bumps version and stages version files
- Does NOT commit, tag, or reinstall (those are separate steps)
- Use `/reinstall` AFTER committing to apply changes to Claude Code
- Version bumps should be part of feature commits (combined), not standalone
