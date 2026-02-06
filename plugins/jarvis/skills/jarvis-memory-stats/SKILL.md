---
name: jarvis-memory-stats
description: Show memory system health and statistics. Use when user says "/memory-stats", "Jarvis, memory status", "how many files indexed", or "memory health".
user_invocable: true
---

# /memory-stats - Memory System Status

Shows the health and statistics of Jarvis's semantic memory system.

## Execution Flow

### 1. Get collection count

Call `mcp__plugin_jarvis_chroma__chroma_get_collection_count` with:

```json
{
  "collection_name": "vault"
}
```

### 2. Peek at sample entries

Call `mcp__plugin_jarvis_chroma__chroma_peek_collection` with:

```json
{
  "collection_name": "vault",
  "limit": 5
}
```

### 3. Present status

```
Memory System Status

Collection: vault
Documents indexed: 142
DB location: ~/.jarvis/memory_db/

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

### 4. Handle empty state

If collection doesn't exist or count is 0:

```
Memory System Status

No documents indexed yet.

Run /memory-index to index your vault for semantic search.
```

## Key Rules

- **Read-only** — only queries ChromaDB, never modifies
- **Quick** — two fast API calls, no heavy processing
