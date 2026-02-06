---
name: jarvis-recall
description: Semantic search over vault content using ChromaDB. Use when user says "Jarvis, recall X", "/recall X", "what did we decide about X", "find notes about X", or asks to search vault semantically.
user_invocable: true
---

# /recall - Semantic Vault Search

Search your vault using meaning, not just keywords. Finds related content even when exact words don't match.

## Execution Flow

### 1. Extract query from user input

The user provides a search topic. Examples:
- `/recall OAuth decisions`
- `Jarvis, what did we discuss about authentication?`
- `recall notes about career goals`

### 2. Query ChromaDB

Call `mcp__plugin_jarvis_chroma__chroma_query_documents` with:

```json
{
  "collection_name": "vault",
  "query_texts": ["<user's search query>"],
  "n_results": 5
}
```

**If the collection doesn't exist or is empty**, suggest:
```
No vault index found. Run /jarvis:jarvis-memory-index to index your vault first.
```

### 3. Present results

Format results clearly:

```
Found N results for "[query]":

1. **[Title]** (distance: 0.XX)
   Path: notes/projects/jarvis-plugin.md
   Preview: [first 150 chars of content]

2. **[Title]** (distance: 0.XX)
   Path: journal/jarvis/2026/01/20260124-entry.md
   Preview: [first 150 chars]

...
```

### 4. Offer to read

```
Want me to read any of these in full? (reply with the number)
```

## Filtering (Optional)

If the user specifies a scope, add `where` clauses:

- "recall from journal" → `{"directory": "journal"}`
- "recall work notes" → `{"directory": "work"}`
- "recall high importance" → `{"importance": "high"}`
- "recall ideas" → `{"type": "idea"}`

Pass as `where` parameter to `chroma_query_documents`.

## Graceful Degradation

If ChromaDB/chroma-mcp is not available:
1. Fall back to `Grep` search across the vault
2. Note: "Semantic search unavailable. Falling back to keyword search."
3. Use `mcp__plugin_jarvis_tools__jarvis_read_vault_file` for results

## Key Rules

- **Read-only** — never modify vault content
- **Respect access control** — don't surface documents/ or people/ results unless user explicitly asks
- **Show distances** — helps user gauge relevance (lower = more relevant)
- **Keep previews short** — 150 chars max, strip frontmatter from preview
