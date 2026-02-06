# Claude Development Guide - Jarvis Plugin

This file contains development instructions and conventions for Claude when working on the Jarvis plugin.

---

## Commit Message Guidelines

### Subject Line (First Line)
- **Imperative mood**: "Add feature" not "Added feature"
- **Start with verb**: Add, Fix, Update, Remove, Refactor
- **Keep under 72 characters**
- **Include scope if helpful**: "Fix jarvis-todoist-agent: remove non-existent tool"

### Common Prefixes

| Prefix | Use For |
|--------|---------|
| Add | New features, files, capabilities |
| Fix | Bug fixes |
| Update | Enhancements to existing features |
| Remove | Deletions |
| Refactor | Code restructuring (no behavior change) |

### Body (Optional but Recommended for Non-Trivial Changes)

- Blank line after subject
- Explain **WHAT** and **WHY**, not HOW
- For version bumps: Include "Version bump: X.Y.Z → A.B.C (patch/minor/major)"

### Combined Commits (Preferred)

Feature + version bump in one commit:

```
Add jarvis-explorer-agent and bump to v0.3.0

New Features:
- Vault-aware exploration agent for search
- Supports vault structure and access control

Version bump: 0.2.2 → 0.3.0 (minor: new agent)
```

Version-only commit (rare - only for hotfix releases):

```
Bump version to 0.3.2

Hotfix release for critical production issue.
Version bump: 0.3.1 → 0.3.2 (patch)
```

---

## Development Workflow

When making plugin changes that require a version bump:

1. **Make code changes** (agents, skills, MCP server, etc.)
2. **`/bump`** - Bump version and stage version files (plugin.json + CLAUDE.md)
3. **`git add <other-files>`** - Stage all other changed files
4. **`git commit -m "Your changes and bump to v0.X.Y"`** - Commit with proper message
5. **`git tag -a v0.X.Y -m "Version 0.X.Y: Description"`** - Tag the commit
6. **`git push && git push --tags`** - Push commits and tags to remote
7. **`/reinstall`** - Clear cache and reinstall plugin
8. **Restart Claude Code** - Required for plugin changes to take effect

**The flow:** changes → bump → commit → tag → push → reinstall → restart

### Pre-Commit Checklist

Before committing plugin changes:

- [ ] Version bumped? (use `/bump` if releasing)
- [ ] All modified files staged? (`git status`)
- [ ] Commit message follows guidelines?
- [ ] No sensitive files? (.env, credentials)

---

## Version Bumping Workflow

**Use `/bump` skill to bump version and stage files for commit.**

### The Rule

1. Make code changes (agents, skills, MCP server, etc.)
2. **Use `/bump`** to:
   - Update version in `plugin/.claude-plugin/plugin.json`
   - Update CLAUDE.md version history (for minor/major)
   - Stage both files automatically
3. Stage other changed files: `git add <files>`
4. Commit changes with proper message
5. **Tag the version commit**: `git tag -a v0.X.Y -m "Version 0.X.Y: Description"`
6. Push: `git push && git push --tags`
7. Reinstall plugin with `/reinstall`

**DO NOT bump version without code changes.** Empty version bumps are not allowed.

**ALWAYS tag version bump commits.** This creates a permanent marker for each release and enables proper version tracking (`git tag --contains <commit>`).

### Semantic Versioning Rules

Use the following criteria to decide bump type:

#### Patch (0.2.x → 0.2.x+1)
Use for:
- Bug fixes
- Small changes
- Documentation updates
- Minor refactoring
- Tool configuration tweaks
- Agent instruction clarifications

#### Minor (0.2.x → 0.3.0)
Use for:
- New features
- New skills/agents/commands
- Workflow changes
- Non-breaking enhancements
- New MCP integrations

#### Major (0.x.x → 1.0.0)
Use for:
- Breaking changes
- Complete workflow redesigns
- **ALWAYS ask user before major bumps**

### Current Version
Check: `plugin/.claude-plugin/plugin.json`

---

## Git Tag Workflow

After committing a version bump:

### 1. Create Annotated Tag

```bash
git tag -a v0.X.Y -m "Version 0.X.Y: Brief description"
```

### 2. Tag Message Convention

Follow this format for tag messages:

```
Version 0.3.1: Fix jarvis-todoist-agent missing tool
Version 0.3.0: Add jarvis-explorer-agent
Version 0.2.2: Add CLAUDE.md development guide
```

### 3. Push Tags

```bash
# Push specific tag
git push origin v0.X.Y

# Or push all tags
git push --tags
```

### 4. Verify

```bash
# Check if HEAD is tagged
git tag --contains HEAD

# List recent tags
git tag -l --sort=-version:refname | head -5

# Show tag details
git show v0.X.Y
```

---

## Plugin Reinstall Workflow

When reinstalling during development (after code changes):

### Step 1: Bump Version
Edit `plugin/.claude-plugin/plugin.json` and increment version according to rules above.

### Step 2: Clean Cache & Reinstall

```bash
# Clear all cached versions
rm -rf ~/.claude/plugins/cache/jarvis-marketplace/jarvis/*

# Update marketplace
claude plugin marketplace update

# Uninstall existing
claude plugin uninstall jarvis@jarvis-marketplace

# Install fresh
claude plugin install jarvis@jarvis-marketplace
```

### Step 3: Restart Claude Code
**Required** - Plugin changes only apply after full restart (not just reload).

---

## When to Reinstall

Reinstall is required after modifying:

- **Agent definitions** - Files in `plugin/agents/*.md`
- **Skills** - Files in `plugin/skills/*/SKILL.md`
- **MCP server code** - Files in `plugin/mcp-server/`
- **Plugin manifest** - `plugin/.claude-plugin/plugin.json`
- **MCP configuration** - `plugin/.mcp.json`
- **System prompt** - `plugin/system-prompt.md`

---

## Troubleshooting Reinstalls

If plugin doesn't load after reinstall:

1. **Verify cache cleared**:
   ```bash
   ls ~/.claude/plugins/cache/jarvis-marketplace/jarvis/
   # Should only show current version
   ```

2. **Check marketplace updated**:
   ```bash
   claude plugin marketplace list
   ```

3. **Verify uninstall completed**:
   ```bash
   claude plugin list
   # Should NOT show jarvis
   ```

4. **Check for errors** in Claude Code logs

5. **Full restart** - Quit and reopen Claude Code (not just reload)

6. **Verify git state**:
   ```bash
   git status
   # Ensure changes are committed
   ```

---

## Development Notes

### Plugin Architecture

- **Agents** - Autonomous sub-agents (journal, audit, todoist)
- **Skills** - User-invocable workflows (slash commands)
- **MCP Server** - `jarvis-tools` provides vault file ops and git operations
- **System Prompt** - Jarvis identity and delegation model

### Key Files

| File | Purpose |
|------|---------|
| `plugin/.claude-plugin/plugin.json` | Plugin manifest (version, name, author) |
| `plugin/system-prompt.md` | Jarvis core identity and constraints |
| `plugin/.mcp.json` | MCP server registration |
| `plugin/mcp-server/` | Python MCP server implementation |
| `plugin/agents/*.md` | Agent definitions with tools/prompts |
| `plugin/skills/*/SKILL.md` | Skill workflows |

### Version History

- **1.2.0** - Scheduling: SCHEDULED mode, schedule management skill, session-start checks, 6-option inbox routing, focus check
- **1.1.0** - Shell integration in setup wizard, jarvis.zsh/jarvis.bash snippets
- **1.0.0** - Modular architecture: split into jarvis, jarvis-todoist, jarvis-strategic plugins
- **0.3.0** - jarvis-explorer-agent (vault-aware search), test framework v1.0, capitalization fixes
- **0.2.1** - MCP rename (jarvis-tools→tools), Todoist workflow simplification, inbox processing enhancements
- **0.2.0** - Initial comprehensive test coverage, audit agent refinements

---

## Quick Commands Reference

```bash
# Reinstall (with cache clear)
rm -rf ~/.claude/plugins/cache/jarvis-marketplace/jarvis/* && \
claude plugin marketplace update && \
claude plugin uninstall jarvis@jarvis-marketplace && \
claude plugin install jarvis@jarvis-marketplace

# Check installed version
cat ~/.claude/plugins/cache/jarvis-marketplace/jarvis/*/plugin.json | grep version

# View agent configuration
cat ~/.claude/plugins/cache/jarvis-marketplace/jarvis/*/agents/jarvis-todoist-agent.md | head -10
```
