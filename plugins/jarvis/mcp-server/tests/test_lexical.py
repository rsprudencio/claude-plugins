"""Unit contracts for the statistical (lexical) recall channel (Phase 1).

Exercises lexeme extraction, IDF-based informative-term selection, injection-
safe sanitization, the row-shaping + cosine derivation of lexical_candidates,
and the config getter through its REAL config key.
"""

from __future__ import annotations

import json
import re

import pytest


# ── Fakes ─────────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows, description=None):
        self._rows = rows
        self.description = description

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Desc:
    def __init__(self, name):
        self.name = name


class _RecordingConn:
    """Answers batched-df/unnest queries from canned data; records every call."""

    def __init__(self, total=0, df_map=None, lexemes=None):
        self.total = total
        self.df_map = df_map or {}
        self.lexemes = lexemes or []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        norm = " ".join(sql.split()).lower()
        if "text[]" in norm:
            # Batched df lookup: params[0] is the bound lexeme array; return one
            # (lexeme, df, total) row per lexeme, exactly like _DF_BATCH_SQL.
            lexemes = list(params[0]) if params and params[0] else []
            return _FakeCursor(
                [(lx, self.df_map.get(lx, 0), self.total) for lx in lexemes]
            )
        if "unnest" in norm:  # extract_query_lexemes
            return _FakeCursor([(lx,) for lx in self.lexemes])
        if "count(*)" in norm and params:  # df query, term is params[0]
            return _FakeCursor([(self.df_map.get(params[0], 0),)])
        if "count(*)" in norm:  # total-rows query
            return _FakeCursor([(self.total,)])
        return _FakeCursor([])


# ── extract_query_lexemes ─────────────────────────────────────────────


def test_extract_query_lexemes_returns_lexeme_column():
    from tools.lexical import extract_query_lexemes

    conn = _RecordingConn(lexemes=["igor", "goal"])
    assert extract_query_lexemes(conn, "goals shared by igor") == ["igor", "goal"]


def test_extract_query_lexemes_empty_query_makes_no_query():
    from tools.lexical import extract_query_lexemes

    conn = _RecordingConn(lexemes=["x"])
    assert extract_query_lexemes(conn, "") == []
    assert conn.executed == []


def test_extract_query_lexemes_caps_input_length():
    from tools.lexical import _MAX_QUERY_CHARS, extract_query_lexemes

    conn = _RecordingConn(lexemes=["x"])
    extract_query_lexemes(conn, "a" * (_MAX_QUERY_CHARS + 5000))
    _, params = conn.executed[-1]
    assert len(params[0]) == _MAX_QUERY_CHARS


# ── informative_terms: df-ratio filter, rarity order, caps, safety ────


def test_informative_terms_filters_by_df_ratio():
    """Rarity self-selects: only igor (df 1/100 = 0.01) clears max_df_ratio."""
    from tools.lexical import informative_terms

    conn = _RecordingConn(
        total=100, df_map={"igor": 1, "goal": 36, "main": 49, "share": 70}
    )
    terms = informative_terms(
        conn, ["share", "main", "goal", "igor"], max_df_ratio=0.10, max_terms=8
    )
    assert terms == ["igor"]


def test_informative_terms_orders_rarest_first_and_caps():
    from tools.lexical import informative_terms

    conn = _RecordingConn(total=1000, df_map={"a": 5, "b": 1, "c": 3, "d": 2})
    terms = informative_terms(
        conn, ["a", "b", "c", "d"], max_df_ratio=0.10, max_terms=2
    )
    # df asc → b(1), d(2), c(3), a(5); cap at 2 keeps the two rarest.
    assert terms == ["b", "d"]


def test_informative_terms_skips_absent_terms():
    from tools.lexical import informative_terms

    conn = _RecordingConn(total=100, df_map={"igor": 1})  # "ghost" → df 0
    terms = informative_terms(conn, ["igor", "ghost"], max_df_ratio=0.5)
    assert terms == ["igor"]


def test_informative_terms_empty_corpus_returns_empty():
    from tools.lexical import informative_terms

    conn = _RecordingConn(total=0, df_map={"igor": 1})
    assert informative_terms(conn, ["igor"]) == []


def test_informative_terms_sanitizes_hostile_lexemes():
    """Hostile prompt lexemes carrying tsquery metacharacters are dropped, and
    only bare [a-z0-9]+ tokens are ever passed as bound array params."""
    from tools.lexical import informative_terms

    conn = _RecordingConn(total=100, df_map={"igor": 1, "x": 1, "y": 1})
    hostile = ["igor'", "|", "!x", "&", "(y:*", ")", "a:b", "igor", "x", "y"]
    terms = informative_terms(conn, hostile, max_df_ratio=1.0, max_terms=10)

    assert set(terms) <= {"igor", "x", "y"}
    # The single batched df query binds lexemes as a text[] array; every element
    # is a sanitized bare token, never inlined into SQL text.
    batched = [
        p for sql, p in conn.executed
        if p and "text[]" in " ".join(sql.split()).lower()
    ]
    assert batched, "expected one batched df query"
    for lexeme in batched[0][0]:
        assert re.match(r"^[a-z0-9]+$", lexeme), lexeme


def test_informative_terms_caps_lexeme_count_before_lookup():
    """Only the first _MAX_DF_LEXEMES unique lexemes are looked up, bounding the
    per-prompt hot path even for an adversarial many-token prompt."""
    from tools.lexical import _MAX_DF_LEXEMES, informative_terms

    lexemes = [f"tok{i}" for i in range(_MAX_DF_LEXEMES + 50)]
    conn = _RecordingConn(total=1000, df_map={lx: 1 for lx in lexemes})
    informative_terms(conn, lexemes, max_df_ratio=1.0, max_terms=1000)

    batched = [
        p for sql, p in conn.executed
        if p and "text[]" in " ".join(sql.split()).lower()
    ]
    assert len(batched) == 1  # ONE round-trip, not one-per-lexeme
    assert len(batched[0][0]) == _MAX_DF_LEXEMES


def test_informative_terms_uses_single_round_trip():
    """df for many lexemes is a single batched query (no per-lexeme COUNT loop)."""
    from tools.lexical import informative_terms

    conn = _RecordingConn(total=100, df_map={"a": 1, "b": 1, "c": 1, "d": 1})
    informative_terms(conn, ["a", "b", "c", "d"], max_df_ratio=1.0, max_terms=8)
    # Exactly one execute call (the batched df lookup).
    assert len(conn.executed) == 1
    assert "text[]" in " ".join(conn.executed[0][0].split()).lower()


def test_build_or_tsquery_only_sanitized_terms():
    from tools.lexical import _build_or_tsquery

    assert _build_or_tsquery(["igor", "goal"]) == "igor | goal"
    # metacharacter-bearing tokens dropped whole; only clean survivors joined.
    assert _build_or_tsquery(["igor'", "|", "goal"]) == "goal"
    assert _build_or_tsquery(["'", "|", "!", "(x:*)"]) is None


# ── lexical_candidates: row shape + true-cosine derivation + binding ──


def test_lexical_candidates_shapes_obsidian_rows_and_derives_distance():
    from tools.lexical import lexical_candidates

    cols = [
        "id", "document", "metadata", "parent_file", "directory", "vault_type",
        "title", "chunk_index", "chunk_total", "chunk_heading",
        "importance_score", "similarity", "_schema",
    ]
    row = (
        "vault::notes/x.md", "body", {}, "notes/x.md", "notes", "note", "T",
        0, 1, "", 0.5, 0.3, "obsidian",
    )

    class _Conn:
        def execute(self, sql, params=None):
            self.last = (sql, params)
            return _FakeCursor([row], [_Desc(c) for c in cols])

    conn = _Conn()
    rows = lexical_candidates(
        conn, ["igor"], schema="obsidian", limit=30, query_embedding=[0.0] * 384
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["_schema"] == "obsidian"
    assert r["similarity"] == 0.3
    assert abs(r["distance"] - 0.7) < 1e-9  # distance = 1 - similarity

    sql, params = conn.last
    # tsquery bound as a parameter, never inlined into SQL text.
    assert "%s::tsquery" in sql
    assert params[1] == "igor" and params[2] == "igor"
    assert params[3] == 30  # limit


def test_lexical_candidates_uses_dimensionless_halfvec_cast():
    """The cosine cast is dimensionless (::halfvec), matching the ANN path, so
    the channel works at any embedding dimension (not just 384)."""
    from tools.lexical import lexical_candidates

    class _Conn:
        def execute(self, sql, params=None):
            self.last = (sql, params)
            return _FakeCursor([], [])

    conn = _Conn()
    lexical_candidates(
        conn, ["igor"], schema="obsidian", limit=30, query_embedding=[0.0] * 768
    )
    sql, _ = conn.last
    assert "::halfvec(384)" not in sql
    assert "::halfvec" in sql


def test_lexical_candidates_scopes_by_user_when_provided():
    """A non-anonymous user adds the same metadata->>'user' predicate the ANN
    path applies, bound as a parameter (no cross-user leak)."""
    from tools.lexical import lexical_candidates

    class _Conn:
        def execute(self, sql, params=None):
            self.last = (sql, params)
            return _FakeCursor([], [])

    conn = _Conn()
    lexical_candidates(
        conn, ["igor"], schema="obsidian", limit=30,
        query_embedding=[0.0] * 384, user="alice",
    )
    sql, params = conn.last
    assert "metadata->>'user' = %s" in sql
    assert "alice" in params

    # Anonymous / None user → no predicate.
    lexical_candidates(
        conn, ["igor"], schema="local", limit=30,
        query_embedding=[0.0] * 384, user="anonymous",
    )
    sql2, params2 = conn.last
    assert "metadata->>'user'" not in sql2
    assert "alice" not in params2


def test_lexical_candidates_local_excludes_chunked_parents():
    """The local lexical SQL excludes chunked parents (searched via their
    chunks in the ANN path), mirroring the ANN exclusion."""
    from tools.lexical import lexical_candidates

    class _Conn:
        def execute(self, sql, params=None):
            self.last = (sql, params)
            return _FakeCursor([], [])

    conn = _Conn()
    lexical_candidates(
        conn, ["igor"], schema="local", limit=30, query_embedding=[0.0] * 384
    )
    sql, _ = conn.last
    assert "NOT EXISTS" in sql
    assert "local.memory_chunks" in sql


def test_lexical_candidates_rejects_unsupported_schema():
    from tools.lexical import lexical_candidates

    class _Conn:
        def execute(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("execute should not run for remote schema")

    assert lexical_candidates(
        _Conn(), ["igor"], schema="remote_team", limit=30, query_embedding=[0.0]
    ) == []


def test_lexical_candidates_empty_terms_returns_empty_without_query():
    from tools.lexical import lexical_candidates

    class _Conn:
        def execute(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("execute should not run with no terms")

    assert lexical_candidates(
        _Conn(), [], schema="obsidian", limit=30, query_embedding=[0.0]
    ) == []


# ── config getter via the REAL config key (mock_config fixture) ───────


def test_get_lexical_config_defaults(mock_config):
    from tools.config import get_lexical_config

    cfg = get_lexical_config()
    assert cfg["enabled"] is True
    assert cfg["max_df_ratio"] == 0.10
    assert cfg["max_terms"] == 8
    assert cfg["candidate_limit"] == 30
    assert cfg["lexical_rerank_slots"] == 10


def test_get_lexical_config_reads_real_key(mock_config):
    import tools.config as config_module

    data = json.loads(mock_config.path.read_text())
    data.setdefault("memory", {})["lexical"] = {"enabled": False, "max_terms": 3}
    mock_config.path.write_text(json.dumps(data))
    config_module.clear_config_cache()

    from tools.config import get_lexical_config

    cfg = get_lexical_config()
    assert cfg["enabled"] is False
    assert cfg["max_terms"] == 3
    # untouched keys keep their defaults
    assert cfg["candidate_limit"] == 30
    assert cfg["lexical_rerank_slots"] == 10


def test_context_enrichment_exposes_bge_logit_threshold_default(mock_config):
    from tools.config import get_context_enrichment_config

    assert get_context_enrichment_config()["bge_logit_threshold"] == -4.0


def test_lexical_candidates_rarest_term_matches_never_flooded_out():
    """Regression (live Igor case): one OR-query capped by IDF-blind ts_rank_cd
    let ~200 goal/main-dense rows flood LIMIT 30 and exclude the two rows
    containing the df=1 term. Per-term sequential fill guarantees the rarest
    term's matches claim seats first."""
    from tools.lexical import lexical_candidates

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows
            self.description = [
                type("D", (), {"name": n})
                for n in ("id", "document", "metadata", "parent_file", "directory",
                          "vault_type", "title", "chunk_index", "chunk_total",
                          "chunk_heading", "importance_score", "similarity", "_schema")
            ]

        def fetchall(self):
            return self._rows

    def row(doc_id, sim):
        return (doc_id, "body", {}, "f.md", "notes", "note", "T", 0, 1, "", 0.5, sim, "obsidian")

    class FakeConn:
        def __init__(self):
            self.queries = []

        def execute(self, sql, params):
            self.queries.append(params)
            term = params[1]
            if term == "igor":
                return FakeCursor([row("vault::mandate.md#chunk-1", 0.77)])
            # 'goal' has hundreds of matches; honors the LIMIT param.
            remaining = params[-1]
            return FakeCursor([row(f"vault::goalish-{i}.md", 0.8) for i in range(remaining)])

    conn = FakeConn()
    rows = lexical_candidates(
        conn, ["igor", "goal"], schema="obsidian", limit=30, query_embedding=[0.0] * 384,
    )

    ids = [r["id"] for r in rows]
    assert "vault::mandate.md#chunk-1" in ids          # rarest term guaranteed
    assert ids[0] == "vault::mandate.md#chunk-1"        # fetched first
    assert len(rows) == 30                              # limit still honored
    assert conn.queries[0][1] == "igor"                 # rarity order preserved
