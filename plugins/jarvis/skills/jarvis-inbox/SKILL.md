---
name: jarvis-inbox
description: Process and organize inbox items. Use when user says "Jarvis, organize my inbox" or "process my inbox".
---

# Inbox Processing Workflow

## Steps

1. **Scan** `paths.inbox` (default: `inbox/`) for unprocessed items

2. **Classify each item** using 6 routing options:

   | Classification | Destination (path name) | Handler |
   |----------------|-------------------------|---------|
   | **Journal entry** | `paths.journal_jarvis` (default: `journal/jarvis/`) | Delegate to jarvis-journal-agent |
   | **Work note** | `paths.work` (default: `work/[slug].md`) | Direct creation with frontmatter |
   | **Personal note** | `paths.notes` (default: `notes/[slug].md`) | Direct creation with frontmatter |
   | **Person/contact** | `paths.people` (default: `people/[name].md`) | Direct creation with frontmatter |
   | **Discard** | (deleted) | Remove from inbox after confirmation |
   | **Skip** | stays in `paths.inbox` | Keep for later processing |

   **For Todoist-origin items** (frontmatter: `source: todoist`):
   - Suggest the most likely classification based on content
   - If item looks like a task that was misclassified, offer to push back to Todoist

   **Note file format** (for work/personal/contact notes):
   ```yaml
   ---
   source: todoist  # or "manual" for non-Todoist items
   created: 2026-02-05
   tags: []
   ---

   # [Title]

   [Content]
   ```

3. **Present plan** to user with review options:

   **Option A: Review each item** (default for small batches <5 items)
   ```
   Item 1/3: "I realized morning routines help my focus"
   Source: Todoist (captured 2 days ago)
   → Proposed: Journal entry

   Classify as:
   1. Journal entry (recommended)
   2. Work note → work/
   3. Personal note → notes/
   4. Person/contact → people/
   5. Discard
   6. Skip for now
   ```

   User can **stop anytime**: "stop review" → remaining items stay in inbox.

   **Option B: Batch approve** (for larger batches ≥5 items)
   ```
   Found 12 items in inbox:

   Proposed actions:
   - Journal: 4 items
   - Work notes: 3 items
   - Personal notes: 3 items
   - Discard: 2 items

   Approve batch? (You can review detailed list or review individually)
   ```

4. **Execute** based on user choice:
   - If individual review: Process item by item, stop on "stop review"
   - If batch approve: Execute all at once, show summary after
   - Route files to correct destinations
   - Create any needed folders

5. **Commit** via `jarvis-audit-agent`:
   ```json
   {
     "operation": "move",
     "description": "Inbox processing: moved N files",
     "files": ["list of affected files"]
   }
   ```

## Inbox Location

Path: `paths.inbox` (default: `inbox/`)

## Important

- Always confirm before deleting anything
- Suggest logical groupings when multiple related items exist
- If an item looks like journal content, offer to create a proper entry
