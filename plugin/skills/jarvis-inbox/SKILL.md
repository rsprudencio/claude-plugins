---
name: jarvis-inbox
description: Process and organize inbox items. Use when user says "Jarvis, organize my inbox" or "process my inbox".
---

# Inbox Processing Workflow

## Steps

1. **Scan** `inbox/` for unprocessed items

2. **Categorize** each item and suggest destination:
   - Notes → `notes/[appropriate-subfolder]/`
   - Journal material → trigger jarvis-journal skill
   - Reference docs → `references/`
   - Temporary/trash → confirm deletion

3. **Present plan** to user:
   - List each file with proposed action
   - Ask for approval or adjustments

4. **Execute** with user approval:
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
