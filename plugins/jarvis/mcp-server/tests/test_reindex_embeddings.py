"""Tests for the atomic namespaced embedding reindexer."""

from unittest.mock import MagicMock

import pytest

from bin.reindex_embeddings import (
    STORES,
    apply_staged,
    require_maintenance_ownership,
    resolve_stores,
    stage_store,
    validate_embeddings,
)


def test_resolve_stores_defaults_to_both_namespaces():
    assert [spec.name for spec in resolve_stores("all")] == ["local", "obsidian"]
    assert resolve_stores("local") == [STORES["local"]]


def test_validate_embeddings_rejects_missing_or_wrong_sized_vectors():
    with pytest.raises(RuntimeError, match="1 vectors for 2 documents"):
        validate_embeddings([[0.0] * 384], 2, 384)
    with pytest.raises(RuntimeError, match="has 3 dimensions"):
        validate_embeddings([[0.0] * 3], 1, 384)


def test_stage_failure_never_updates_live_table():
    conn = MagicMock()
    select_cursor = MagicMock()
    select_cursor.fetchall.return_value = [("memory::1", "first")]
    conn.execute.side_effect = [MagicMock(), MagicMock(), select_cursor]
    service = MagicMock()
    service.encode.side_effect = RuntimeError("host unavailable")
    service.encode_batch.side_effect = RuntimeError("host unavailable")

    with pytest.raises(RuntimeError, match="host unavailable"):
        stage_store(conn, STORES["local"], service, dimensions=384, batch_size=16)

    sql = " ".join(call.args[0] for call in conn.execute.call_args_list)
    assert "UPDATE local.memories" not in sql


def test_maintenance_ownership_fails_before_staging_for_read_only_role():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = ("jarvis", "postgres", False)
    conn.execute.return_value = cursor
    with pytest.raises(RuntimeError, match="connected as jarvis, table owner is postgres"):
        require_maintenance_ownership(conn, [STORES["local"]])


def _count_cursor(value):
    cursor = MagicMock()
    cursor.fetchone.return_value = (value,)
    return cursor


def test_apply_is_atomic_and_preserves_timestamps_by_disabling_update_trigger():
    conn = MagicMock()
    transaction = MagicMock()
    conn.transaction.return_value = transaction

    update_cursor = MagicMock(rowcount=2)
    meta_cursor = MagicMock()
    conn.execute.side_effect = [
        MagicMock(),  # LOCK memories
        MagicMock(),  # LOCK chunks
        _count_cursor(2),  # live count
        _count_cursor(2),  # staged count
        MagicMock(),  # DISABLE TRIGGER
        update_cursor,
        MagicMock(),  # DELETE chunks
        MagicMock(),  # INSERT chunks from staging
        MagicMock(),  # ENABLE TRIGGER
        MagicMock(),  # LOCK uncovered obsidian store (SHARE)
        _count_cursor(0),  # uncovered obsidian store is empty
        meta_cursor,
    ]

    result = apply_staged(
        conn,
        [(STORES["local"], 2)],
        model_identity="ibm-granite/granite-embedding-small-english-r2",
        dimensions=384,
        backend="host",
    )

    assert result == {"local.memories": 2}
    sql = [call.args[0] for call in conn.execute.call_args_list]
    assert any("DISABLE TRIGGER trg_local_memories_updated_at" in item for item in sql)
    assert any("ENABLE TRIGGER trg_local_memories_updated_at" in item for item in sql)
    assert transaction.__enter__.called and transaction.__exit__.called


def test_partial_apply_never_relabels_meta_while_other_store_holds_vectors():
    """A --store local run must not disarm check_model_consistency for obsidian."""
    conn = MagicMock()
    conn.transaction.return_value = MagicMock()

    conn.execute.side_effect = [
        MagicMock(),  # LOCK memories
        MagicMock(),  # LOCK chunks
        _count_cursor(2),  # live count
        _count_cursor(2),  # staged count
        MagicMock(),  # DISABLE TRIGGER
        MagicMock(rowcount=2),  # UPDATE embeddings
        MagicMock(),  # DELETE chunks
        MagicMock(),  # INSERT chunks from staging
        MagicMock(),  # ENABLE TRIGGER
        MagicMock(),  # LOCK uncovered obsidian store (SHARE)
        _count_cursor(7),  # obsidian.documents still holds old-space vectors
    ]

    apply_staged(
        conn,
        [(STORES["local"], 2)],
        model_identity="new-model",
        dimensions=384,
        backend="host",
    )

    sql = " ".join(call.args[0] for call in conn.execute.call_args_list)
    assert "INSERT INTO local.meta" not in sql
    # The emptiness probe must hold a writer-blocking lock so "empty" cannot
    # flip between the count and the (skipped) relabel.
    assert "LOCK TABLE obsidian.documents IN SHARE MODE" in sql


def test_full_apply_records_new_model_identity_in_meta():
    conn = MagicMock()
    conn.transaction.return_value = MagicMock()

    conn.execute.side_effect = [
        # local store
        MagicMock(),  # LOCK memories
        MagicMock(),  # LOCK chunks
        _count_cursor(1),
        _count_cursor(1),
        MagicMock(),  # DISABLE TRIGGER
        MagicMock(rowcount=1),
        MagicMock(),  # DELETE chunks
        MagicMock(),  # INSERT chunks from staging
        MagicMock(),  # ENABLE TRIGGER
        # obsidian store
        MagicMock(),  # LOCK documents
        _count_cursor(1),
        _count_cursor(1),
        MagicMock(),  # DISABLE TRIGGER
        MagicMock(rowcount=1),
        MagicMock(),  # ENABLE TRIGGER
        MagicMock(),  # meta INSERT
    ]

    apply_staged(
        conn,
        [(STORES["local"], 1), (STORES["obsidian"], 1)],
        model_identity="new-model",
        dimensions=384,
        backend="host",
    )

    meta_calls = [
        call for call in conn.execute.call_args_list
        if "INSERT INTO local.meta" in call.args[0]
    ]
    assert len(meta_calls) == 1
    assert '"model": "new-model"' in meta_calls[0].args[1][0]


def test_obsidian_staging_embeds_context_augmented_text(monkeypatch):
    """Re-embedding stored raw chunk text would silently strip the document-
    context prefix the live index path adds (tools/chunk_context.py) and
    diverge the embedding space. The obsidian branch must augment."""
    import tools.config as config

    monkeypatch.setattr(config, "get_contextual_embeddings_enabled", lambda: True)

    conn = MagicMock()
    select_cursor = MagicMock()
    select_cursor.fetchall.return_value = [
        ("vault::notes/a.md#chunk-0", "raw fragment text", "My Note", "Section", 3, "notes/a.md"),
        ("vault::notes/whole.md", "whole doc text", "Whole", "", 1, "notes/whole.md"),
    ]
    conn.execute.side_effect = [MagicMock(), select_cursor, MagicMock()]

    captured = {}
    service = MagicMock()

    def capture_batch(texts, batch_size=16):
        captured["texts"] = list(texts)
        return [[0.0] * 384 for _ in texts]

    service.encode_batch.side_effect = capture_batch

    stage_store(conn, STORES["obsidian"], service, dimensions=384, batch_size=16)

    chunk_text, whole_text = captured["texts"]
    assert chunk_text.startswith("Document: notes/a.md")
    assert "My Note" in chunk_text and "Section" in chunk_text
    assert chunk_text.endswith("raw fragment text")
    # Whole-document rows are never prefixed (they begin with their own title).
    assert whole_text == "whole doc text"
    # Staging stores only ids + vectors — raw text is never modified anywhere.
    sql = " ".join(str(c.args[0]) for c in conn.execute.call_args_list)
    assert "UPDATE obsidian.documents" not in sql


# ── Achieved augmentation state ───────────────────────────────────────
#
# This script READS the summary cache and never generates. With an empty cache
# it therefore re-embeds mechanically — and it used to then stamp local.meta
# with the CONFIGURED mode ('summary'), disarming check_model_consistency
# forever. The startup warning even recommended running it, so following the
# code's own advice permanently hid the fact that the feature was inert.


def _obsidian_conn(rows, summary_rows):
    conn = MagicMock()
    select_cursor = MagicMock()
    select_cursor.fetchall.return_value = rows
    summary_cursor = MagicMock()
    summary_cursor.fetchall.return_value = summary_rows

    def execute(sql, params=None, *args, **kwargs):
        normalized = " ".join(str(sql).split()).lower()
        if "from obsidian.document_context" in normalized:
            return summary_cursor
        if "select id, document" in normalized:
            return select_cursor
        return MagicMock()

    conn.execute.side_effect = execute
    return conn


def _stage_obsidian(monkeypatch, summary_rows):
    import tools.config as config

    monkeypatch.setattr(config, "get_contextual_augmentation_mode", lambda: "summary")
    monkeypatch.setattr(
        config, "get_contextual_summaries_config",
        lambda: {"enabled": True, "max_chars": 200},
    )
    rows = [
        ("vault::notes/a.md#chunk-0", "frag a", "A", "S", 3, "notes/a.md"),
        ("vault::notes/b.md#chunk-0", "frag b", "B", "S", 2, "notes/b.md"),
        ("vault::notes/whole.md", "whole", "W", "", 1, "notes/whole.md"),
    ]
    conn = _obsidian_conn(rows, summary_rows)
    service = MagicMock()
    service.encode_batch.side_effect = lambda texts, batch_size=16: [
        [0.0] * 384 for _ in texts
    ]
    coverage = {}
    stage_store(
        conn, STORES["obsidian"], service, dimensions=384, batch_size=16,
        coverage=coverage,
    )
    return coverage


def test_stage_store_counts_summary_coverage_per_file(monkeypatch):
    coverage = _stage_obsidian(
        monkeypatch,
        [("notes/a.md", "A situating sentence naming Igor as manager.", "h")],
    )
    # Whole-document rows are never augmented, so they are not candidates.
    assert coverage["chunked_files"] == {"notes/a.md", "notes/b.md"}
    assert coverage["files_with_summary"] == {"notes/a.md"}


def test_stage_store_reports_zero_coverage_for_an_empty_cache(monkeypatch):
    coverage = _stage_obsidian(monkeypatch, [])
    assert coverage["chunked_files"] == {"notes/a.md", "notes/b.md"}
    assert coverage.get("files_with_summary", set()) == set()


def test_stage_store_coverage_is_optional(monkeypatch):
    """Callers that do not care (and the byte-identity test) must still work."""
    import tools.config as config

    monkeypatch.setattr(config, "get_contextual_augmentation_mode", lambda: "summary")
    rows = [("vault::notes/a.md#chunk-0", "frag", "A", "S", 3, "notes/a.md")]
    conn = _obsidian_conn(rows, [])
    service = MagicMock()
    service.encode_batch.side_effect = lambda texts, batch_size=16: [
        [0.0] * 384 for _ in texts
    ]
    assert stage_store(
        conn, STORES["obsidian"], service, dimensions=384, batch_size=16
    ) == 1


def test_apply_staged_records_the_supplied_state_not_the_config(monkeypatch):
    """The honest-identity fix, at the SQL boundary."""
    import json

    import tools.config as config

    monkeypatch.setattr(config, "get_contextual_augmentation_mode", lambda: "summary")

    conn = MagicMock()
    counts = iter([1, 1, 0])  # live count, staged count, other-store count

    def execute(sql, params=None, *args, **kwargs):
        normalized = " ".join(str(sql).split()).lower()
        cursor = MagicMock()
        if "select count(*)" in normalized:
            cursor.fetchone.return_value = (next(counts),)
        cursor.rowcount = 1
        return cursor

    conn.execute.side_effect = execute
    conn.transaction.return_value = MagicMock(
        __enter__=lambda self: self, __exit__=lambda self, *a: False
    )

    apply_staged(
        conn, [(STORES["obsidian"], 1)],
        model_identity="m", dimensions=384, backend="mock",
        contextual_state="mechanical",
        contextual_coverage={"chunked_files": 12, "files_with_summary": 0},
    )
    payloads = [
        json.loads(call.args[1][0])
        for call in conn.execute.call_args_list
        if call.args and len(call.args) > 1 and call.args[1]
        and "embedding_config" in " ".join(str(call.args[0]).split())
    ]
    assert payloads, "embedding_config was never recorded"
    record = payloads[-1]
    assert record["contextual_chunks"] == "mechanical"
    assert record["contextual_coverage"] == {
        "chunked_files": 12, "files_with_summary": 0,
    }
