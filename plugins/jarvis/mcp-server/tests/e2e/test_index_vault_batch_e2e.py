"""E2E: index_vault batch isolation — one bad row must not drop its siblings.

Regression guard for the silent data-loss bug: a single unparseable frontmatter
timestamp aborted the whole _upsert_batch transaction, silently dropping up to
_BATCH_SIZE-1 sibling files while index_vault still reported success=True with an
inflated files_indexed count and one misattributed error.

The bad file here uses a value that is unparseable regardless of timestamp-regex
sanitization, so this guards the general "one bad row in a batch" class — not just
the timezone-suffix case.
"""

import os

import psycopg
import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("E2E_POSTGRES_URL"),
        reason="E2E_POSTGRES_URL not set",
    ),
]

# Good files omit `created` entirely → _sanitize_timestamp falls back to now()
# (always a valid timestamptz). Only the bad file carries a poison timestamp,
# isolating the variable under test.
GOOD_DOC = """---
type: note
---
# Note {n}

Body content for note {n} with enough words to embed cleanly.
"""

BAD_DOC = """---
type: note
created: totally-not-a-timestamp
---
# Bad File

This file has an unparseable `created` value that PostgreSQL rejects on
INSERT (::timestamptz cast), which previously aborted the whole batch.
"""

# Multiple ## sections (> min_chunk_chars=200 total) → indexes as >1 chunk, so
# it exercises the partial-commit cleanup path: all chunks share the bad
# timestamp and fail, and any committed siblings must be removed.
BAD_MULTICHUNK_DOC = """---
type: note
created: totally-not-a-timestamp
---
## Section One

This is the first section, with enough prose to form its own chunk when the
document is split at heading boundaries during indexing of the vault.

## Section Two

The second section likewise carries sufficient text to be embedded as a
distinct chunk, ensuring this file produces more than one chunk overall.

## Section Three

A third section so the document is unambiguously multi-chunk, exercising the
partial-commit cleanup path in _flush_batch end to end.
"""


def _count(db_url: str, parent_file: str) -> int:
    conn = psycopg.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM obsidian.documents WHERE parent_file = %s",
                (parent_file,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_index_vault_isolates_bad_row(e2e_config):
    """A single invalid-timestamp file must not drop its batch siblings, and
    files_indexed must reflect only the rows that actually persisted."""
    from tools.memory import index_vault

    vault_dir = e2e_config["vault_dir"]
    db_url = e2e_config["db_url"]
    notes = vault_dir / "notes"

    # 12 good + 1 bad → more than _BATCH_SIZE (10), so the bad row is guaranteed
    # to share at least one flushed batch with good rows.
    good_paths = []
    for i in range(12):
        name = f"good_{i:02d}.md"
        (notes / name).write_text(GOOD_DOC.format(n=i))
        good_paths.append(f"notes/{name}")

    (notes / "bad.md").write_text(BAD_DOC)

    result = index_vault(force=True)

    # The call still completes successfully overall...
    assert result["success"] is True

    # ...every good file is persisted (the bug dropped whole batches)...
    missing = [p for p in good_paths if _count(db_url, p) < 1]
    assert not missing, f"good files lost to a poisoned batch: {missing}"

    # ...the bad file is absent (its row was rolled back)...
    assert _count(db_url, "notes/bad.md") == 0

    # ...the reported count is honest — only the 12 good files...
    assert result["files_indexed"] == 12

    # ...and the failure is attributed to the real file, not a sibling.
    error_files = {e.get("file") for e in result.get("errors", [])}
    assert "notes/bad.md" in error_files


def test_index_vault_bad_multichunk_leaves_no_partial(e2e_config):
    """A multi-chunk file with a bad timestamp must leave ZERO chunks behind
    (clean absence, not half-indexed), so a later non-force run re-attempts it
    rather than treating a partially-indexed file as already done."""
    from tools.memory import index_vault

    vault_dir = e2e_config["vault_dir"]
    db_url = e2e_config["db_url"]
    notes = vault_dir / "notes"

    (notes / "good.md").write_text(GOOD_DOC.format(n=0))
    (notes / "bad_multi.md").write_text(BAD_MULTICHUNK_DOC)

    result = index_vault(force=True)
    assert result["success"] is True
    # Good file persisted...
    assert _count(db_url, "notes/good.md") >= 1
    # ...bad multi-chunk file left no partial state...
    assert _count(db_url, "notes/bad_multi.md") == 0
    # ...honest count (only the good file)...
    assert result["files_indexed"] == 1
    # ...and it is reported by its real path.
    assert "notes/bad_multi.md" in {e.get("file") for e in result["errors"]}

    # Self-healing: a non-force re-run must NOT skip the bad file as "already
    # indexed" (it has no rows), so it is retried and still reports cleanly.
    result2 = index_vault(force=False)
    assert _count(db_url, "notes/bad_multi.md") == 0
    assert "notes/bad_multi.md" in {e.get("file") for e in result2["errors"]}
