---
name: jarvis-memory-index
description: Bulk index vault files into ChromaDB for semantic search. Use when user says "/memory-index", "Jarvis, index my vault", "rebuild memory index", or "reindex vault".
user_invocable: true
---

# /memory-index - Vault Memory Indexing

Indexes all .md files in your vault into ChromaDB for semantic search via `/recall`.

## Execution Flow

### 1. Parse options from user input

Detect flags from natural language:
- `--force` or "reindex everything" → `force: true`
- `--dir notes/` or "only index notes" → `directory: "notes"`
- `--sensitive` or "include private files" → `include_sensitive: true`

### 2. Run indexing

Call `mcp__plugin_jarvis_tools__jarvis_index_vault` with detected options:

```json
{
  "force": false,
  "directory": null,
  "include_sensitive": false
}
```

### 3. Report results

```
Vault indexed successfully!

Files indexed: 142
Files skipped: 8 (templates, already indexed)
Errors: 0
Duration: 3.2s
Total in collection: 142

Your vault is now searchable with /recall.
```

If errors occurred, list them:
```
Indexing completed with errors:
- notes/broken-file.md: UTF-8 decode error
```

## First-Time Guidance

If the collection was empty before indexing (new setup):
```
First-time indexing complete! Your vault is now searchable.

Try it: /recall [any topic]

The index will auto-update as you create journal entries.
For a full re-index later: /memory-index --force
```

## Key Rules

- **Safe operation** — indexing is read-only on vault files, writes only to ~/.jarvis/memory_db/
- **Idempotent** — running twice without --force skips already-indexed files
- **No sensitive by default** — documents/ and people/ are excluded unless --sensitive
- **Templates excluded** — always skips templates/ and .obsidian/
