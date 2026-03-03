---
name: jarvis-sync
description: Manage multi-remote memory sync — view status, force sync, retry failures, and validate routing rules.
user_invocable: true
user_triggers:
  - /jarvis-sync
  - jarvis sync status
  - sync status
  - check sync
  - retry sync
  - sync force
---

# Jarvis Sync Management

Manage the multi-remote sync engine that routes memories to configured PostgreSQL remotes.

## Commands

### `jarvis sync status` (default)
Show current sync state:
1. Read sync config via `get_sync_config()` from tools
2. If sync disabled → report "Sync is disabled" and exit
3. Call `get_queue_stats()` to get per-destination counts
4. Present summary:
   - Enabled remotes (names only, never show URLs)
   - Pending / sending / done / failed / DLQ counts per remote
   - Overall health assessment

### `jarvis sync force [remote]`
Force immediate drain of the sync queue:
1. If `remote` specified, only process that destination
2. Call `_sync_iteration()` directly (not the background loop)
3. Report results

### `jarvis sync retry-dlq [remote]`
Re-queue dead-letter entries for retry:
1. Call `retry_dlq(pool, destination)` from sync_queue
2. Report how many entries were reset to pending

### `jarvis sync lint`
Dry-run rule evaluation against sample memories:
1. Load routing rules from config
2. Call `validate_sync_config()` — report any errors
3. Fetch 10 recent memories from core.memories
4. Evaluate routing for each, show which destinations each would route to
5. Highlight any memories with no route (local-only by default)

### `jarvis sync sweep`
Retroactively route existing memories that predate rule creation:
1. Requires explicit user confirmation (destructive-ish: creates many queue entries)
2. Load all active local-origin memories
3. Evaluate routing for each
4. Enqueue sync entries for unsynced destinations
5. Report total entries created

## Security
- Never display remote URLs or credentials in output
- Use `redact_dsn()` from sync_config if URL display is ever needed
