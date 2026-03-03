---
name: jarvis-consolidate
description: Browse and consolidate redundant memories via LLM-driven clustering
user_invocable: true
trigger: "/jarvis-consolidate"
trigger_aliases:
  - "consolidate memories"
  - "memory consolidation"
  - "jarvis, consolidate"
  - "clean up memories"
---

<jarvis-consolidate>

# Memory Consolidation

LLM-driven consolidation finds groups of redundant memories and synthesizes them into authoritative summaries.

## Commands

### 1. Dry Run (default)
Show what would be consolidated without making changes.

```
/jarvis-consolidate
/jarvis-consolidate dry-run
```

**Output**: List of memory clusters with size, similarity, importance, and previews.

### 2. Interactive
Step through each cluster, approve or reject consolidation.

```
/jarvis-consolidate interactive
```

For each cluster:
1. Show memory contents and similarity score
2. Call LLM to generate consolidated summary
3. Show summary + confidence score + any contradictions
4. Ask user: approve / reject / edit

### 3. Undo
Reverse a consolidation run.

```
/jarvis-consolidate undo <run_id>
```

Restores all superseded originals and soft-deletes the consolidated summaries.

## Workflow

1. Call `find_consolidation_candidates()` — ANN-based clustering
2. For each cluster:
   a. Load full contents
   b. Run secret scanner on cluster contents
   c. Build consolidation prompt, call LLM
   d. Parse response, assess confidence
   e. Route based on mode (dry-run / interactive / auto)
3. For approved consolidations: transactional apply
4. Report results

## Safety

- **Contradictions flagged, never auto-resolved** — human reviews via interactive mode
- **Originals preserved** — superseded, not deleted
- **Reversible** — undo by `consolidation_run_id`
- **Secret scanning** — cluster contents checked before LLM call
- **Disabled by default** — `memory.consolidation.enabled: false`

</jarvis-consolidate>
