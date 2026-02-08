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

### 2. Query vault memory

Call `mcp__plugin_jarvis_core__jarvis_retrieve` with:

```json
{
  "query": "<user's search query>",
  "n_results": 5
}
```

**If the collection is empty**, the tool returns a message suggesting `/memory-index`.

### 3. Present results

Format results clearly, handling both Tier 1 (file-backed) and Tier 2 (ephemeral) results:

```
Found N results for "[query]":

1. **[Title]** (relevance: 0.XX)
   Path: notes/projects/jarvis-plugin.md
   Type: note | Importance: high
   Preview: [150 char preview]

2. **[Title]** (relevance: 0.XX)
   Source: observation (auto-generated)
   Type: observation | Importance: 0.75
   Preview: [150 char preview]

3. **[Title]** (relevance: 0.XX)
   Path: journal/jarvis/2026/01/20260124-entry.md
   Type: journal | Importance: medium
   Preview: [150 char preview]

...
```

**Tier 2 results**: When `tier == "chromadb"`, show `Source: [type] (auto-generated)` instead of `Path:`. These are ephemeral documents (observations, patterns, summaries).

### 4. Offer to read or promote

```
Want me to read any of these in full? (reply with the number)

[If any Tier 2 results are shown:]
Want me to save any Tier 2 items permanently? (reply with number to promote)
```

## Filtering (Optional)

If the user specifies a scope, add `filter`:

- "recall from journal" → `{"directory": "journal"}`
- "recall work notes" → `{"directory": "work"}`
- "recall high importance" → `{"importance": "high"}`
- "recall ideas" → `{"type": "idea"}`

Pass as `filter` parameter to `jarvis_retrieve(query=...)`.



## Graceful Degradation

If `jarvis_retrieve` is unavailable or returns an error:
1. Fall back to `Grep` search across the vault
2. Note: "Semantic search unavailable. Falling back to keyword search."
3. Use `mcp__plugin_jarvis_core__jarvis_read_vault_file` for results

## Key Rules

- **Read-only** — never modify vault content
- **Respect access control** — don't surface documents/ or people/ results unless user explicitly asks
- **Show relevance scores** — helps user gauge result quality (higher = more relevant)
- **Keep previews short** — 150 chars max, frontmatter already stripped by jarvis_retrieve
