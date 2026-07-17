# Codex Support

Jarvis supports local Codex CLI, the Codex IDE extension, and the Codex desktop
experience when they run on the same host as Jarvis. The local HTTP MCP servers
listen on `localhost`, so hosted Codex cloud tasks are not supported by this
setup.

## Install

Prerequisites:

- Codex CLI
- Docker with Compose

For a complete Codex-only setup, including Jarvis configuration and the local
Docker service:

```bash
curl -fsSL https://raw.githubusercontent.com/rsprudencio/jarvis/refs/heads/master/install.sh \
  | JARVIS_HARNESS=codex bash
```

The installer skips the Claude-specific launcher and statusline when Codex is
selected. If Jarvis is already configured and running, install only the plugin.

Add the marketplace and install the core plugin:

```bash
codex plugin marketplace add rsprudencio/jarvis
codex plugin add jarvis@jarvis-plugins
codex plugin add jarvis-obsidian@jarvis-plugins
```

Optional plugins:

```bash
codex plugin add jarvis-todoist@jarvis-plugins
codex plugin add jarvis-strategic@jarvis-plugins
```

`jarvis-toolbelt` remains Claude-only because its agents and DAR launcher use
Claude-specific protocols. It is intentionally absent from the Codex
marketplace until those workflows are ported.

Restart the Codex host after installation. In Codex CLI, open `/hooks`, review
the Jarvis command hooks, and trust them. Hook trust is tied to the exact hook
definition, so changed hooks must be reviewed again.

Start a new thread after installing or upgrading. Existing threads do not
rebuild their skill and tool inventory automatically.

## Verify

Confirm plugin and MCP discovery:

```bash
codex plugin list --marketplace jarvis-plugins
codex mcp list
curl -sf http://localhost:8741/health
curl -sf http://localhost:8742/health
curl -sf http://localhost:8744/health
```

The MCP list should include `core`, `api`, and `vault` when the corresponding
plugins are installed.

## Validate Memory Injection

Enable `memory.context_enrichment.debug` temporarily in
`~/.jarvis/config.json`, then use a fresh Codex thread for each probe:

1. Submit half of a known memory and expect that memory at rank 1.
2. Paraphrase the same memory and expect it to remain above the quality gate.
3. Submit a nearby-but-wrong prompt and expect no injection.
4. Submit an unrelated prompt and expect no injection.

Inspect `~/.jarvis/debug.per-prompt-search.log` for the exact injected context,
source IDs, scores, latency, and explicit empty outcomes. A fresh thread avoids
Jarvis's intentional per-session reinjection deduplication.

## Upgrade

After a new Jarvis version is available in the marketplace:

```bash
codex plugin marketplace upgrade jarvis-plugins
codex plugin add jarvis@jarvis-plugins
```

Repeat `codex plugin add` for any installed extensions, restart Codex, review
changed hooks, and start a new thread.

## Harness Boundaries

- Codex and Claude use separate plugin manifests but share the same skills,
  local MCP services, hook business logic, and Jarvis configuration.
- Codex hook output uses `hookSpecificOutput.additionalContext`; Claude keeps
  its compatible plain-context output.
- Auto-extraction normalizes prompt and completion data before passing it to
  the shared observation pipeline.
- Codex-only passive auto-extraction currently requires `ANTHROPIC_API_KEY`;
  without it, keep auto-extract disabled or retain Claude CLI as the fallback
  extraction provider. Memory retrieval and injection do not require either.
  The full installer disables `memory.auto_extract.mode` automatically when no
  provider is available; set that key to `background` after configuring one.
- The Claude Code statusline is not installed into Codex. Jarvis health remains
  available through MCP tools and the settings skill.
