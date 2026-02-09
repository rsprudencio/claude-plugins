---
name: jarvis-promote
description: Browse and promote Tier 2 (ephemeral) content to permanent vault files. Use when user says "/jarvis-promote", "Jarvis, promote this", "save observation permanently", "review tier 2 content", or "what observations do I have".
user_invocable: true
---

# /jarvis-promote - Tier 2 Content Management

Browse, preview, and promote ephemeral Tier 2 content (observations, patterns, summaries) to permanent file-backed Tier 1 storage.

## Execution Flow

### 1. Parse user intent

Determine mode from input:
- `/jarvis-promote` or `/jarvis-promote browse` → **Browse mode** (list Tier 2 content)
- `/jarvis-promote preview <id>` or user replies with a number → **Preview mode**
- `/jarvis-promote confirm <id>` or user confirms after preview → **Promote mode**
- `/jarvis-promote auto` → **Auto-promote** all items meeting criteria

### 2. Browse mode (default)

Ask user how to filter:

```
AskUserQuestion:
  questions:
    - question: "Filter Tier 2 content?"
      header: "Filter"
      options:
        - label: "Show all"
          description: "All Tier 2 content regardless of type"
        - label: "Observations only"
          description: "Auto-extracted insights from sessions"
        - label: "Patterns only"
          description: "Detected behavioral patterns"
        - label: "High importance (>=0.7)"
          description: "Only items scoring 0.7+ importance"
      multiSelect: false
```

Call `mcp__plugin_jarvis_core__jarvis_retrieve` with `list_type="tier2"` and appropriate filters:

- "Show all" → `{"list_type": "tier2", "limit": 20}`
- "Observations only" → `{"list_type": "tier2", "type_filter": "observation", "limit": 20}`
- "Patterns only" → `{"list_type": "tier2", "type_filter": "pattern", "limit": 20}`
- "High importance" → `{"list_type": "tier2", "min_importance": 0.7, "limit": 20}`

Present results as a numbered table:

```
Tier 2 Content (N items):

 #  | Type        | Imp.  | Retrievals | Age  | Preview
----|-------------|-------|------------|------|-----------------------------
 1  | observation | 0.65  | 2          | 3d   | OAuth flow uses PKCE with...
 2* | pattern     | 0.88  | 5          | 12d  | User prefers kebab-case...
 3  | observation | 0.45  | 0          | 1d   | Vault structure has 3 main...
 4* | summary     | 0.72  | 4          | 8d   | Week focused on memory...

* = Meets promotion criteria
```

Then ask what to do next:

```
AskUserQuestion:
  questions:
    - question: "What would you like to do?"
      header: "Action"
      options:
        - label: "Preview an item"
          description: "View full content and promotion criteria"
        - label: "Auto-promote eligible"
          description: "Promote all items marked with *"
        - label: "Done"
          description: "Exit /jarvis-promote"
      multiSelect: false
```

If "Preview an item", ask user which number. If "Auto-promote eligible", proceed to auto-promote mode (Step 5).

**To determine promotion eligibility for the `*` marker**, evaluate each item against:
- Importance >= 0.85 (default, from `~/.jarvis/config.json` → `promotion.importance_threshold`)
- Retrievals > 3 (default, from `promotion.retrieval_count_threshold`)
- Age >= 30 days AND importance >= 0.7 (from `promotion.age_importance_days` + `promotion.age_importance_score`)

Any ONE criterion met = eligible.

**Calculate age** from `created_at` metadata field vs current time.

**Preview text**: First 60 characters of content, trimmed to last word boundary, with `...` suffix.

### 3. Preview mode

When user picks a number or provides an ID, call `mcp__plugin_jarvis_core__jarvis_retrieve`:

```json
{
  "id": "<tier2-id>"
}
```

Display full details with promotion criteria evaluation:

```
Preview: pattern::user-prefers-kebab-case

Content:
  User consistently uses kebab-case for file naming across vault
  notes, journal entries, and config files. Corrected when
  alternatives suggested.

Type: pattern | Importance: 0.88 | Retrievals: 5 | Age: 12d
Source: auto-extract:stop-hook

Promotion Criteria (from ~/.jarvis/config.json):
  [checkmark] Importance 0.88 >= threshold 0.85
  [checkmark] Retrievals 5 > threshold 3
    Age 12d < 30d (age criterion not met, but not needed)

Target path: [resolved from content type - see path resolution below]
```

Then ask for confirmation:

```
AskUserQuestion:
  questions:
    - question: "Promote this item to permanent storage?"
      header: "Promote"
      options:
        - label: "Yes, promote"
          description: "Write to vault as permanent file"
        - label: "No, go back"
          description: "Return to browse list"
      multiSelect: false
```

**Path resolution by type:**
- `observation` → resolves via `observations_promoted` path name
- `pattern` → resolves via `patterns_promoted` path name
- `summary` → resolves via `summaries_promoted` path name
- `learning` → resolves via `learnings_promoted` path name
- `decision` → resolves via `decisions_promoted` path name

Read `~/.jarvis/config.json` to get promotion thresholds for display. If no `promotion` section exists, show defaults: importance 0.85, retrievals 3, age 30d + importance 0.7.

### 4. Promote mode

When user confirms, call `mcp__plugin_jarvis_core__jarvis_promote`:

```json
{
  "doc_id": "<tier2-id>"
}
```

On success, show result and offer git commit:

```
Promoted to: [promoted_path from response]
```

```
AskUserQuestion:
  questions:
    - question: "Commit promoted file to vault?"
      header: "Commit"
      options:
        - label: "Yes, commit now"
          description: "Delegate to jarvis-audit-agent"
        - label: "No, I'll commit later"
          description: "File is written but uncommitted"
      multiSelect: false
```

If user confirms commit, delegate to `jarvis-audit-agent`:
- operation: `create`
- description: `Promote [type]: [short title from content]`
- files: `[promoted_path]`

### 5. Auto-promote mode

When user says `/jarvis-promote auto`:

1. Call `mcp__plugin_jarvis_core__jarvis_retrieve` with `{"list_type": "tier2", "limit": 100}` to get all Tier 2 content
2. Evaluate each item against promotion criteria (same logic as browse `*` marker)
3. Filter to only promotable types: observation, pattern, summary, learning, decision
4. Show eligible items:

```
Auto-Promote Scan:

Found N items meeting promotion criteria:
 1. pattern::user-prefers-kebab-case (importance 0.88, 5 retrievals)
 2. summary::week-2026-w06 (importance 0.72, 4 retrievals)
```

```
AskUserQuestion:
  questions:
    - question: "Promote all N eligible items?"
      header: "Auto-Promote"
      options:
        - label: "Yes, promote all"
          description: "Promote all eligible items to vault"
        - label: "Let me pick"
          description: "Go back to browse and select individually"
        - label: "Cancel"
          description: "Don't promote anything"
      multiSelect: false
```

5. If confirmed, promote each sequentially via `jarvis_promote`
6. After all promotions, offer a **single batch commit**:

```
Promoted N items:
  - [path1]
  - [path2]
```

```
AskUserQuestion:
  questions:
    - question: "Commit all promoted files to vault?"
      header: "Commit"
      options:
        - label: "Yes, commit all"
          description: "Single batch commit via jarvis-audit-agent"
        - label: "No, I'll commit later"
          description: "Files are written but uncommitted"
      multiSelect: false
```

Delegate batch commit to `jarvis-audit-agent` with all promoted paths.

## Key Rules

- **Preview before action** — always show what will happen before promoting
- **Respect supported types** — only observation, pattern, summary can be promoted. For other types (code, relationship, hint, plan), show: "Type '[type]' can't be promoted yet. Supported: observation, pattern, summary, learning, decision."
- **Show config thresholds** — in preview mode, display the actual threshold values from config so users understand the criteria and know they can tune them
- **Batch commits** — for auto-promote, offer one commit for all promotions, not per-item
- **Idempotent** — if an item is already promoted, show "Already promoted to [path]" — not an error
- **No auto-commit** — always ask before committing. User may want to review first.
- **Configurable everything** — mention `~/.jarvis/config.json` paths when showing thresholds so users know where to tune

## Graceful Degradation

- If `jarvis_retrieve(list_type="tier2")` returns empty → "No Tier 2 content found. Auto-Extract captures observations automatically during sessions. Check mode with /jarvis-memory-stats."
- If `jarvis_promote` fails for unsupported type → "Type '[type]' can't be promoted yet. Supported: observation, pattern, summary, learning, decision."
- If `jarvis_promote` returns error → Show error message, suggest checking `/jarvis-memory-stats` for system health
- If ChromaDB unavailable → "Memory system unavailable. Try /jarvis-settings to configure and index your vault."
- If config has no `promotion` section → Use defaults (importance 0.85, retrievals 3, age 30d)
