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
   - Update version in `plugins/jarvis/.claude-plugin/plugin.json` (and optionally other plugin manifests)
   - Update CLAUDE.md version history (for minor/major)
   - Stage version files automatically
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
Check: `plugins/jarvis/.claude-plugin/plugin.json` (core plugin version)

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
Edit `plugins/jarvis/.claude-plugin/plugin.json` (and other affected plugin manifests) and increment version according to rules above.

### Step 2: Clean Cache & Reinstall

**Preferred:** Use **`/reinstall`** skill - handles everything automatically.

Manual alternative:
```bash
rm -rf ~/.claude/plugins/cache/raph-claude-plugins/jarvis/*
claude plugin marketplace update
claude plugin uninstall jarvis@raph-claude-plugins
claude plugin install jarvis@raph-claude-plugins
```

### Step 3: Restart Claude Code
**Required** - Plugin changes only apply after full restart (not just reload).

---

## When to Reinstall

Reinstall is required after modifying:

- **Agent definitions** - Files in `plugins/*/agents/*.md`
- **Skills** - Files in `plugins/*/skills/*/SKILL.md`
- **MCP server code** - Files in `plugins/jarvis/mcp-server/`
- **Plugin manifests** - `plugins/*/.claude-plugin/plugin.json`
- **MCP configuration** - `plugins/jarvis/.mcp.json`
- **System prompts** - `plugins/*/system-prompt.md`

---

## Troubleshooting Reinstalls

If plugin doesn't load after reinstall:

1. **Verify cache cleared**:
   ```bash
   ls ~/.claude/plugins/cache/raph-claude-plugins/jarvis/
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

### Plugin Architecture (Modular Marketplace)

The plugin is split into 3 independent plugins in a single marketplace:

- **`plugins/jarvis/`** - Core: MCP server, agents (journal, audit, explorer), core skills
- **`plugins/jarvis-todoist/`** - Optional: Todoist agent + skills
- **`plugins/jarvis-strategic/`** - Optional: Strategic analysis skills

### Key Files

| File | Purpose |
|------|---------|
| `.claude-plugin/marketplace.json` | Marketplace manifest (all plugins) |
| `plugins/jarvis/.claude-plugin/plugin.json` | Core plugin manifest (version, name) |
| `plugins/jarvis/system-prompt.md` | Jarvis core identity and constraints |
| `plugins/jarvis/.mcp.json` | MCP server registration |
| `plugins/jarvis/mcp-server/` | Python MCP server (14 tools) |
| `plugins/jarvis/agents/*.md` | Core agent definitions |
| `plugins/jarvis/skills/*/SKILL.md` | Core skill workflows |
| `plugins/jarvis-todoist/agents/*.md` | Todoist agent definition |
| `plugins/jarvis-todoist/skills/*/SKILL.md` | Todoist skill workflows |
| `plugins/jarvis-strategic/skills/*/SKILL.md` | Strategic skill workflows |

### Version History

- **1.9.0** - Two-Tier SSoT architecture: Tier 2 (ChromaDB-first) ephemeral content, 5 new MCP tools (tier2_write/read/list/delete, promote), 7 content types (observation, pattern, summary, code, relationship, hint, plan), smart promotion based on importance/retrieval/age, tier-aware query results, 3 new namespaces (rel::, hint::, plan::), 54 new tests (30 total tools)
- **1.8.0** - Configurable paths: centralized path resolution via `tools/paths.py` replacing all hardcoded vault paths, 2 new MCP tools (jarvis_resolve_path, jarvis_list_paths), template variable substitution ({YYYY}/{MM}/{WW}), sensitive path detection, 45 new tests (25 total tools)
- **1.7.0** - Remove Serena dependency: replace all Serena MCP references across 14 files in 3 plugins with native jarvis_memory_* tools, strategic memories now file-backed at .jarvis/strategic/, read-modify-write pattern replaces serena_edit_memory (jarvis-strategic 1.1.0, jarvis-todoist 1.3.0)
- **1.6.0** - Memory CRUD tools: 4 new file-backed memory tools (jarvis_memory_write/read/list/delete), secret detection scanner, rename jarvis_memory_read→jarvis_doc_read and jarvis_memory_stats→jarvis_collection_stats with detailed mode, recency boost in query scoring (23 total tools)
- **1.5.0** - Unified collection & namespaces: ChromaDB `jarvis` collection with namespaced IDs (vault:: prefix), enriched metadata schema (universal type/namespace/timestamps + vault_type), tools/namespaces.py module
- **1.4.0** - Chroma-MCP consolidation: absorb 3 chroma-mcp tools into jarvis-tools (jarvis_query, jarvis_memory_read, jarvis_memory_stats), remove chroma-mcp dependency, rename MCP server tools→core
- **1.3.0** - ChromaDB semantic memory: /recall, /memory-index, /memory-stats skills, vault-wide indexing, explorer semantic pre-search, config migration to ~/.jarvis/
- **1.2.0** - Scheduling: SCHEDULED mode, schedule management skill, session-start checks, 6-option inbox routing, focus check
- **1.1.0** - Shell integration in setup wizard, jarvis.zsh/jarvis.bash snippets
- **1.0.0** - Modular architecture: split into jarvis, jarvis-todoist, jarvis-strategic plugins
- **0.3.0** - jarvis-explorer-agent (vault-aware search), test framework v1.0, capitalization fixes
- **0.2.1** - MCP rename (jarvis-tools→tools), Todoist workflow simplification, inbox processing enhancements
- **0.2.0** - Initial comprehensive test coverage, audit agent refinements

---

## Quick Commands Reference

**Preferred: Use `/reinstall` skill** - handles cache clear, marketplace update, uninstall, and reinstall automatically.

```bash
# Manual reinstall (if /reinstall unavailable)
rm -rf ~/.claude/plugins/cache/raph-claude-plugins/jarvis/* && \
claude plugin marketplace update && \
claude plugin uninstall jarvis@raph-claude-plugins && \
claude plugin install jarvis@raph-claude-plugins

# Check installed version
cat ~/.claude/plugins/cache/raph-claude-plugins/jarvis/*/plugin.json | grep version

# View agent configuration
cat ~/.claude/plugins/cache/raph-claude-plugins/jarvis/*/agents/jarvis-journal-agent.md | head -10
```
