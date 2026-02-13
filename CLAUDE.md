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
| `plugins/jarvis/mcp-server/` | Python MCP server (21 tools) |
| `plugins/jarvis/agents/*.md` | Core agent definitions |
| `plugins/jarvis/skills/*/SKILL.md` | Core skill workflows |
| `plugins/jarvis-todoist/agents/*.md` | Todoist agent definition |
| `plugins/jarvis-todoist/skills/*/SKILL.md` | Todoist skill workflows |
| `plugins/jarvis-strategic/skills/*/SKILL.md` | Strategic skill workflows |
| `docker/Dockerfile` | Multi-stage Docker image build |
| `docker/entrypoint.sh` | Process manager for containerized servers |
| `docker/docker-compose.yml` | Compose template for dev/reference |
| `.github/workflows/docker-publish.yml` | CI: build & push to GHCR on tags |
| `.github/workflows/docker-test.yml` | CI: test Docker image on PRs |

### Docker Development

Both MCP servers have `http_app.py` alongside `server.py` — thin ASGI wrappers using `StreamableHTTPSessionManager` from MCP SDK. Key facts:

- **Transport:** Streamable HTTP with `json_response=True` (not SSE)
- **Architecture:** Raw ASGI app (no Starlette) to avoid `/mcp` → `/mcp/` 307 redirects
- **Ports:** jarvis-core on 8741, jarvis-todoist on 8742
- **Config:** `JARVIS_HOME` and `JARVIS_VAULT_PATH` env vars override config.json paths

```bash
# Build locally
docker build -f docker/Dockerfile -t jarvis-local .

# Run integration tests (requires image built)
python3 -m pytest docker/tests/ -v

# Test manually
docker run -d -p 8741:8741 -v /tmp/vault:/vault -v ~/.jarvis:/config \
  -e JARVIS_HOME=/config -e JARVIS_VAULT_PATH=/vault jarvis-local
curl http://localhost:8741/health
```

### Version History

- **1.27.0** - Budget-based per-prompt injection with vault references: replace per-item `max_content_length` truncation with total character budget (default 8000, split 50/50 between tier2 and vault), vault items shown as compact references (path + heading, ~120ch) instead of truncated content, tier2 items (observations, learnings) shown with full content, budget overflow from unused half to the other, `semantic_context()` signature simplified to `(query, threshold, budget)`, JSONL telemetry at `~/.jarvis/telemetry/prompt_search.jsonl` for ongoing threshold/budget analysis, config key consolidation (`budget_tier2`/`budget_vault` → single `budget`), updated across 8 files (query.py, prompt_search.py, config.py, defaults/config.json, capabilities.json, SKILL.md, tests, user config), 1073 tests passing
- **1.26.0** - Standalone jarvis executable + Docker auto-start: replace shell function injection (`jarvis.bash`/`jarvis.zsh`) with unified `jarvis.sh` standalone executable installed to PATH (`~/.local/bin/jarvis`), installer auto-cleans old `# Jarvis AI Assistant START/END` markers from RC files, `jarvis-transport.sh` auto-starts Docker container on `container` mode (compose up + 15s health check), auto-stops on `local` mode, remote health check on `remote` mode, updated SKILL.md/capabilities.json/README.md references (shell function → executable)
- **1.25.0** - MCP transport mode switching: `mcp_transport` config key (`local`/`container`/`remote`), `mcp_remote_url` for remote Docker hosts, server early-exit pattern (stdio servers `sys.exit(0)` when transport != local), `jarvis-transport.sh` standalone helper script (status/local/container/remote commands), `/jarvis-settings` transport menu option, `get_mcp_transport()`/`get_mcp_remote_url()` config getters, installer Docker flow uses transport helper, docs updated (docker/README.md switching section, capabilities.json, README.md). jarvis-todoist bumped to v1.5.1 (early-exit support). 2 new files, 10 modified, 6 new tests (1149 total across both plugins).
- **1.24.0** - Docker distribution: Streamable HTTP transport layer (`http_app.py` raw ASGI wrappers with `json_response=True`, no Starlette to avoid 307 redirects), multi-stage Dockerfile (Python 3.12 + uv deps + git/curl runtime), `entrypoint.sh` process manager (health checks, graceful shutdown, conditional Todoist), docker-compose.yml, `JARVIS_HOME`/`JARVIS_VAULT_PATH`/`TODOIST_API_TOKEN` env var overrides in config.py + paths.py + todoist_api.py, Docker option in `install.sh` (detection, compose generation, container management helper), GitHub Actions CI/CD (docker-publish on tags for multi-platform amd64+arm64, docker-test on PRs), `get_verified_vault_path()` bug fix (now uses `get_vault_path()` to respect env vars), 12 new files + 7 modified, 5 http_app tests + 7 Docker integration tests (1143 total across both plugins). jarvis-todoist bumped to v1.5.0.
- **1.23.0** - Configurable file format support (Markdown / Org-mode): new `format_support.py` central abstraction module, `file_format` config key (`"md"` or `"org"`), Org-mode parsers (`:PROPERTIES:` drawers, `*` headings, `#+BEGIN_SRC` blocks), `jarvis_get_format_reference` MCP tool for agents to load format templates at runtime, format-aware indexing/chunking/querying across 10 source files, installer + settings format selection, format reference files (`defaults/formats/`), 40+ new tests (1055 total)
- **1.22.1** - Fix Todoist tool name prefix (underscore → hyphen to match Claude Code plugin name resolution across 5 files); bump jarvis-todoist to v1.4.2 (1057 total tests)
- **1.22.0** - Native Todoist API: add dedicated MCP server to jarvis-todoist plugin via official `todoist-api-python` SDK (local stdio, no session drops), eliminate external HTTP MCP dependency (ai.todoist.net/mcp), 9 tools (find_tasks, find_tasks_by_date, add_tasks, complete_tasks, update_tasks, delete_object, user_info, find_projects, add_projects), SDK singleton + cached inbox resolution, tool name migration across 5 files (agent, 3 skills, system-prompt), `todoist.api_token` config key in defaults/config.json, 68 todoist tests + 989 core tests (1057 total). jarvis-todoist bumped to v1.4.2.
- **1.21.0** - Multi-turn session extraction: replace single-turn `pick_best_turn` with session-level pipeline — `filter_substantive_turns` (all qualifying turns), `extract_first_user_message` (conversation opener context), `compute_content_budget` (dynamic scaling from output token volume), `build_session_prompt` (proportional budget allocation across turns), `SESSION_EXTRACTION_PROMPT` (numbered turns + array response schema), `normalize_extraction_response` (backward-compatible new/legacy schema), Haiku `max_tokens` 300→800, `max_observations` config key (default 3, capped per extraction), main() rewrite for multi-observation loop with per-obs storage, 44 new tests (961 total)
- **1.20.0** - Per-session watermark tracking for auto-extract: replace global 120s cooldown with per-session line watermarks (`~/.jarvis/state/sessions/<id>.json`), forward multi-turn parser (`parse_all_turns`) + best-turn scorer (`pick_best_turn`), `read_transcript_from()` replaces `tail -N`, atomic watermark writes via tempfile+os.replace, SessionStart hook for stale watermark cleanup (>30 days), `max_transcript_lines` default 100→500, remove `cooldown_seconds` config key entirely, simplified `stop-extract.sh` (no temp files), 44 new tests (917 total)
- **1.19.0** - Config template SSoT + AI-first docs: `defaults/config.json` as single source of truth for all config keys (replaces duplicated heredoc in install.sh and inline JSON in SKILL.md), `capabilities.json` moved into plugin distribution for self-reference, README rewrite as human-friendly quickstart, install.sh reads config from template with Python substitution, stale `/jarvis-setup` references fixed in config.py error messages, system-prompt self-reference pointer to capabilities.json, pre-commit checklist updated with stale-ref check
- **1.18.0** - Project-aware auto-extract & promote: extract file paths from tool_use blocks in transcript turns, Haiku scope classification (project vs global), `relevant_files` + `scope` metadata in observations, `sort_by` parameter for tier2_list (importance_desc default, importance_asc, created_at_desc/asc, none), sort_by plumbed through retrieve API + server schema, project-aware promotion routing (nests under `<type>_promoted/<project_dir>/`), enriched promotion frontmatter (scope, project, files fields), updated /jarvis-promote SKILL.md (Project column, sorted browse, preview context), 30 new tests (873 total)
- **1.17.0** - Per-prompt semantic search: automatic vault memory injection via `UserPromptSubmit` hook, `semantic_context()` search function with threshold filtering + sensitive dir exclusion + no retrieval count increment, prompt filtering (skip trivial/short/commands/confirmations), XML-formatted context output, `get_per_prompt_config()` config getter, single-Python-process hook pipeline (~250-500ms), configurable threshold/max_results/content_length, system prompt + settings skill updates, 32 new tests (841 total)
- **1.16.0** - Markdown chunking + importance scoring + query expansion: hybrid heading/paragraph chunking for per-section embeddings, 0.0-1.0 importance scoring from content signals (type weight, concept patterns, recency decay, retrieval frequency), rule-based query expansion with synonym mappings and intent detection, chunk deduplication in query results, 3 new config getters (chunking/scoring/expansion), backward-compatible with unchunked documents, 96 new tests (806 total)
- **1.15.0** - Installer + settings redesign: rewrite `install.sh` as curl-pipe-bash installer with prereq validation (Python 3.10+, uv/uvx, Claude CLI), MCP server verification, full config write with all ~30 defaults visible; rename `/jarvis-setup` → `/jarvis-settings` as menu-driven re-runnable config manager; delete `/memory-index` skill (folded into install.sh + settings); update all references across 12 files (14 user-invocable skills)
- **1.14.0** - Unified Content API: consolidate 14 write/read/delete tools into 3 (`jarvis_store`, `jarvis_retrieve`, `jarvis_remove`) with namespace-based routing, retrieve-mutate-reindex closed loop, auto-index on vault writes, `topics`→`tags` taxonomy cleanup, `learning`/`decision` content types, observation project context enrichment (project_dir, git_branch), remove TYPE_* aliases (21 total tools)
- **1.13.0** - `/promote` skill for Tier 2 content management (browse/preview/promote/auto-promote), auto-extract configuration in setup wizard with progressive disclosure (3 presets + custom), system prompt updates for discoverability
- **1.12.0** - Stop hook redesign: PostToolUse → Stop hook for conversation-turn-level observation, transcript JSONL parsing, substance/cooldown thresholds, drop inline mode, debug logging support
- **1.11.0** - Multi-mode background extraction: smart fallback (API → CLI), `background-api` (Anthropic SDK, needs API key), `background-cli` (Claude CLI via OAuth), refactored extraction into `call_haiku_api`/`call_haiku_cli`/`_parse_haiku_text` helpers, 30s timeout for CLI, mode-aware prerequisites health check, 35 new tests (577 total)
- **1.10.0** - Auto-Extract: passive observation capture from tool calls into Tier 2 memory, PostToolUse hook with 3 modes (disabled/background/inline), filtering module with anti-recursion skip lists and SHA-256 dedup, Haiku-based extraction for background mode, inline systemMessage for session model extraction, user-configurable skip list overrides, 53 new tests (542 total)
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
