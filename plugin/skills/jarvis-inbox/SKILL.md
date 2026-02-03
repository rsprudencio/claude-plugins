---
name: jarvis-inbox
description: Process and organize inbox items. Use when user says "Jarvis, organize my inbox" or "process my inbox".
---

# Inbox Processing Workflow

## Steps

1. **Scan** `inbox/` for unprocessed items

2. **Categorize** each item and suggest destination:

   **For Todoist-origin items** (check frontmatter: `source: todoist`):
   - Journal material → trigger jarvis-journal skill (creates journal entry, deletes inbox file)
   - Still a task → push back to Todoist project (creates task, deletes inbox file)
   - Notes/reference → move to `notes/` or `references/`
   - Outdated/irrelevant → confirm deletion

   **For other inbox items**:
   - Notes → `notes/[appropriate-subfolder]/`
   - Journal material → trigger jarvis-journal skill
   - Reference docs → `references/`
   - Temporary/trash → confirm deletion

3. **Present plan** to user with review options:

   **Option A: Review each item** (default for small batches <5 items)
   ```
   Found 3 items in inbox:

   1. "morning-routines.md" (Todoist)
      → Proposed: Journal entry
      → Alternative: Keep as note | Push to Todoist | Delete

   2. "jarvis-architecture.md" (Todoist)
      → Proposed: Keep as note (notes/ideas/)
      → Alternative: Journal | Push to Todoist | Delete

   3. "meeting-notes.md"
      → Proposed: Keep as note (notes/meetings/)
      → Alternative: Journal | Delete

   Approve all? Or review individually?
   ```

   **Option B: Batch approve** (for larger batches ≥5 items)
   ```
   Found 12 items in inbox:

   Proposed actions:
   - Journal: 4 items
   - Notes: 6 items
   - Delete: 2 items

   Approve batch? (You can review detailed list if needed)
   ```

4. **Execute** based on user choice:
   - If individual review: Process item by item with confirmations
   - If batch approve: Execute all at once, show summary after
   - Move files to destinations
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

Path: `inbox/`

## Important

- Always confirm before deleting anything
- Suggest logical groupings when multiple related items exist
- If an item looks like journal content, offer to create a proper entry
