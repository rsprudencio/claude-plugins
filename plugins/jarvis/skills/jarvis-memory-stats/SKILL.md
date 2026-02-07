---
name: jarvis-memory-stats
description: Show memory system health and statistics. Use when user says "/memory-stats", "Jarvis, memory status", "how many files indexed", or "memory health".
user_invocable: true
---

# /memory-stats - Memory System Status

Shows the health and statistics of Jarvis's semantic memory system.

## Execution Flow

### 1. Get stats

Call `mcp__plugin_jarvis_core__jarvis_memory_stats` with:

```json
{
  "sample_size": 5
}
```

### 2. Present status

```
Memory System Status

Collection: vault
Documents indexed: 142
DB location: ~/.jarvis/memory_db/  (configurable via memory.db_path in ~/.jarvis/config.json)

Sample entries:
- notes/projects/jarvis-plugin.md (type: note)
- journal/jarvis/2026/01/20260124-entry.md (type: journal)
- notes/career/goals.md (type: note)
- work/kubernetes-setup.md (type: work)
- journal/jarvis/2026/02/20260206-chromadb.md (type: journal)

Commands:
  /recall <query>       - Search your vault semantically
  /memory-index         - Re-index vault files
  /memory-index --force - Full re-index (rebuilds everything)
```

### 3. Handle empty state

If total_documents is 0:

```
Memory System Status

No documents indexed yet.

Run /memory-index to index your vault for semantic search.
```

## Key Rules

- **Read-only** — only queries ChromaDB, never modifies
- **Quick** — single API call, no heavy processing
