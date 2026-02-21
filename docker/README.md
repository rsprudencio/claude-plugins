# Jarvis Docker Deployment

Run the Jarvis MCP servers in a Docker container. No Python, uv, or ChromaDB compilation needed on your host machine.

## Quick Start

```bash
# 1. Pull the image
docker pull ghcr.io/rsprudencio/jarvis:latest

# 2. Create config
mkdir -p ~/.jarvis
cat > ~/.jarvis/config.json << 'EOF'
{"vault_path": "/vault", "vault_confirmed": true}
EOF

# 3. Start the container
docker compose -f docker/docker-compose.yml up -d

# 4. Verify
curl http://localhost:8741/health
# → {"status":"ok","server":"jarvis-core","version":"1.28.5"}
```

Or use the installer: `bash install.sh` and choose **[2] Docker**.

## Architecture

```
Host Machine
├── Claude Code
│   ├── Plugin (skills, agents, MCP instructions) ← installed via marketplace
│   ├── MCP config → http://localhost:8741/mcp, http://localhost:8742/mcp
│   └── Hooks (prompt_search, extract_observation) → ChromaDB :8743
│
└── Docker Container
    ├── ChromaDB      (port 8743) — Semantic memory database
    ├── jarvis-core   (port 8741) — Vault ops, memory, git audit
    ├── jarvis-todoist (port 8742) — Todoist API (if token configured)
    └── Volumes:
        ├── /vault  ← your Obsidian/markdown vault
        └── /config ← ~/.jarvis/ (config + ChromaDB data)
```

The container runs 3 processes managed by `entrypoint.sh`. ChromaDB starts first; once its heartbeat is healthy, jarvis-core and jarvis-todoist launch. All ChromaDB access goes through HTTP — both MCP servers and host-side hooks connect to port 8743.

Both MCP servers use **Streamable HTTP** transport (MCP SDK). Claude Code connects via `"type": "http"` URL-based MCP config.

## Claude Code MCP Configuration

After starting the container, tell Claude Code to connect via HTTP:

```bash
claude mcp add --transport http jarvis-core http://localhost:8741/mcp
claude mcp add --transport http jarvis-todoist-api http://localhost:8742/mcp
```

Or add to your settings JSON manually:

```json
{
  "mcpServers": {
    "jarvis-core": { "type": "http", "url": "http://localhost:8741/mcp" },
    "jarvis-todoist-api": { "type": "http", "url": "http://localhost:8742/mcp" }
  }
}
```

## Volume Mounts

| Container Path | Host Path | Purpose |
|---|---|---|
| `/vault` | Your Obsidian vault | Markdown/Org files, journal entries |
| `/config` | `~/.jarvis/` | Config, ChromaDB database, state |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `JARVIS_HOME` | `/config` | Config directory inside container |
| `JARVIS_VAULT_PATH` | `/vault` | Vault directory inside container |
| `TODOIST_API_TOKEN` | (empty) | Todoist API token; enables jarvis-todoist on port 8742 |
| `JARVIS_AUTOCRLF` | `false` | Set `true` for Windows hosts (git line ending conversion) |
| `JARVIS_CORE_PORT` | `8741` | Port for jarvis-core |
| `JARVIS_TODOIST_PORT` | `8742` | Port for jarvis-todoist |
| `CHROMA_PORT` | `8743` | Port for ChromaDB server |
| `CHROMA_HOST` | `127.0.0.1` | ChromaDB bind address (inside container) |

## Management

The installer creates `~/.jarvis/jarvis-docker.sh`:

```bash
~/.jarvis/jarvis-docker.sh status   # Container status
~/.jarvis/jarvis-docker.sh logs     # Follow logs
~/.jarvis/jarvis-docker.sh restart  # Restart services
~/.jarvis/jarvis-docker.sh update   # Pull latest image + restart
~/.jarvis/jarvis-docker.sh stop     # Stop container
```

## Windows Notes

Docker is the **recommended** installation method for Windows:

- No Python compilation issues (ChromaDB's native deps are pre-built in the image)
- No uv/uvx installation needed
- Set `JARVIS_AUTOCRLF=true` to handle CRLF line endings in vault files
- Use forward slashes in volume paths: `-v C:/Users/you/vault:/vault`

With Docker Desktop and WSL2:
```bash
docker compose -f ~/.jarvis/docker-compose.yml up -d
```

## Troubleshooting

### Port already in use

```bash
# Check what's using port 8741
lsof -i :8741  # macOS/Linux
netstat -ano | findstr :8741  # Windows

# Change ports in docker-compose.yml:
# ports:
#   - "9741:8741"
#   - "9742:8742"
```

### Container won't start

```bash
docker compose -f ~/.jarvis/docker-compose.yml logs
```

Common issues:
- Volume path doesn't exist on host
- Port conflict with another service
- Missing config.json (create minimal one — see Quick Start)

### ChromaDB startup slow

First startup with an empty database takes ~5-10 seconds for ChromaDB initialization. Subsequent starts are faster. The healthcheck has a 20-second start period to accommodate ChromaDB + jarvis-core startup sequence.

### Hooks in Docker mode

Claude Code hooks (session-cleanup, prompt-search, stop-extract) run on the **host**, not inside the container. They connect directly to ChromaDB on port 8743 via `HttpClient` — no extra configuration needed as long as port 8743 is exposed.

Requirements for hooks with Docker:
1. **Python 3.10+ on host** with `chromadb` package installed (hooks import from plugin source)
2. ChromaDB port (8743) accessible from host (default in docker-compose.yml)

## Building Locally

```bash
# From repo root
docker build -f docker/Dockerfile -t jarvis-local .

# Run with local image
docker run -d --name jarvis \
  -p 8741:8741 -p 8742:8742 -p 8743:8743 \
  -v ~/my-vault:/vault \
  -v ~/.jarvis:/config \
  -e JARVIS_HOME=/config \
  -e JARVIS_VAULT_PATH=/vault \
  jarvis-local
```

## Upgrading

```bash
# Pull latest
docker pull ghcr.io/rsprudencio/jarvis:latest

# Restart with new image
docker compose -f ~/.jarvis/docker-compose.yml up -d
```

Or use the helper: `~/.jarvis/jarvis-docker.sh update`
