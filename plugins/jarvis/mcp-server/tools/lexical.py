"""Statistical (lexical) recall channel for hybrid retrieval — Phase 1.

A recall-only companion to the bi-encoder ANN channel. The bi-encoder misses
identity-indirection queries (proper nouns, exact tokens, "my manager Igor");
rare prompt terms self-select via document frequency (high IDF), and a
full-text OR-query over the generated tsvector columns surfaces the documents
the embedding space ranks outside the ANN window.

Design invariants (see CLAUDE.md 3.4.0 history — score incommensurability is
this codebase's recurring bug class):

* Channels are UNIONED, never blended. ``ts_rank_cd`` is used ONLY as the
  ``LIMIT`` tiebreak; it is not a relevance score downstream.
* Every lexical row carries its TRUE raw cosine (``1 - pgvector_distance``) so
  it is commensurable with ANN rows for unified scoring + telemetry.
* This channel makes NO reject decision. All rejection belongs to the final
  judge (the cosine threshold OR the augmented-BGE logit gate).

SECURITY: lexemes derive from USER PROMPTS. Building tsquery strings by
concatenation would allow tsquery/SQL injection, so lexemes are filtered to
``^[a-z0-9]+$`` (everything else dropped) AND passed only as bound parameters
cast to ``tsquery`` — never formatted into SQL text.

Schemas: ``obsidian`` and ``local`` only. Remote mirror schemas (``remote_*``)
are Phase 2 (their metadata filtering + read-only semantics need separate
handling). Metadata filters are likewise Phase 2 — when a caller supplies a
metadata filter the lexical union is SKIPPED entirely by the caller (never
silently ignored), and the channel gates only on liveness (``status = 'active'``
for local; obsidian has no status column, per the v3.3.8 fix). Per-user
isolation IS honored: a non-anonymous ``user`` adds the same
``metadata->>'user'`` predicate the ANN path applies.

Corpus statistics (document frequency) are computed over the UNION of both
searchable schemas at FILE granularity (obsidian: DISTINCT parent_file; local:
one active memory == one file), so an empty vault does not disable the channel
for local memories, and a term concentrated in one many-chunk file (or present
only in local memories) stays informative.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("jarvis-core")

# Cap the raw prompt handed to to_tsvector. Lexeme extraction is cheap but the
# input is untrusted and unbounded (whole pasted transcripts); bound it.
_MAX_QUERY_CHARS = 4000

# Hard cap on the number of unique lexemes whose document frequency is looked
# up. The df lookup is a SINGLE batched round-trip (see informative_terms), but
# an adversarial prompt of thousands of unique tokens would still make that one
# query scan the GIN index once per lexeme; bounding the array keeps the
# per-prompt hot path predictable. The rarest survivors are chosen from this
# capped set — a modest cap because informative_terms keeps at most max_terms
# anyway (default 8).
_MAX_DF_LEXEMES = 48

# A safe lexeme is a bare alphanumeric token. to_tsvector output for English is
# already lowercased + stemmed; anything with tsquery metacharacters
# (' | & ! ( ) : *) or punctuation is rejected outright.
_SAFE_LEXEME = re.compile(r"^[a-z0-9]+$")

# Only these schemas carry a tsv column in Phase 1.
_SUPPORTED_SCHEMAS = ("obsidian", "local")


def extract_query_lexemes(conn, query: str) -> list[str]:
    """Return the English-config lexemes of ``query`` in document order.

    Uses ``unnest(to_tsvector(...))`` so the lexemes are stemmed/stop-worded
    exactly as the stored ``tsv`` columns were, making later document-frequency
    matches exact. The query is bound (never interpolated) and capped at
    ``_MAX_QUERY_CHARS``.
    """
    if not query:
        return []
    text = query[:_MAX_QUERY_CHARS]
    cur = conn.execute(
        "SELECT lexeme FROM unnest(to_tsvector('english', %s))",
        (text,),
    )
    rows = cur.fetchall()
    return [row[0] for row in rows if row and row[0] is not None]


def _sanitize_lexemes(lexemes: list[str]) -> list[str]:
    """Drop anything that is not a bare ``[a-z0-9]+`` token, preserve order."""
    safe: list[str] = []
    for lexeme in lexemes:
        if isinstance(lexeme, str) and _SAFE_LEXEME.match(lexeme):
            safe.append(lexeme)
    # De-duplicate while preserving first-seen order.
    return list(dict.fromkeys(safe))


# One batched round-trip for every lexeme's document frequency.
#
# * df is counted at FILE granularity, not chunk granularity: obsidian counts
#   DISTINCT parent_file (falling back to id for rows without one), so a term
#   concentrated in one many-chunk file (e.g. an "Igor" title indexed into all
#   90 chunks of a 1:1s note) stays rare (1 file) instead of looking common
#   (90 chunks).
# * The corpus is the UNION of both searchable schemas — obsidian.documents
#   (file granularity) and active local.memories (one memory == one file). An
#   empty obsidian vault therefore no longer disables the channel for local
#   memories, and a term present only in local memories still has df > 0.
# * All lexemes ride in as a single bound ``text[]`` array (never inlined), so
#   this is ONE round-trip regardless of prompt length — the per-lexeme COUNT
#   loop on the synchronous injection hot path is gone.
_DF_BATCH_SQL = (
    "SELECT t.lexeme, cnt.df, "
    "  ( (SELECT count(DISTINCT COALESCE(parent_file, id)) FROM obsidian.documents) "
    "  + (SELECT count(*) FROM local.memories WHERE status = 'active') ) AS total "
    "FROM unnest(%s::text[]) AS t(lexeme) "
    "CROSS JOIN LATERAL ( "
    "  SELECT "
    "    ( (SELECT count(DISTINCT COALESCE(o.parent_file, o.id)) "
    "       FROM obsidian.documents o WHERE o.tsv @@ t.lexeme::tsquery) "
    "    + (SELECT count(*) FROM local.memories m "
    "       WHERE m.status = 'active' AND m.tsv @@ t.lexeme::tsquery) ) AS df "
    ") AS cnt"
)


def informative_terms(
    conn,
    lexemes: list[str],
    *,
    max_df_ratio: float = 0.10,
    max_terms: int = 8,
) -> list[str]:
    """Select the rare, informative lexemes from a query.

    Document frequency (df) is counted at FILE granularity over the UNION of
    ``obsidian.documents`` (DISTINCT parent_file, falling back to id) and active
    ``local.memories`` (one row == one file); a lexeme is kept only when
    ``df / total_files <= max_df_ratio``. Survivors are ordered by df ascending
    (rarest — most informative — first) and capped at ``max_terms``.

    Rarity self-selects the informative term: on a live vault the prompt
    "goals my manager Igor shared" yields df igor=1 (IDF 5.81) while manager,
    share, goal, main are common; only ``igor`` survives.

    PERFORMANCE: every lexeme's df is looked up in a SINGLE batched query (one
    round-trip), and the sanitized lexeme set is capped at ``_MAX_DF_LEXEMES``
    before the lookup so an adversarial many-token prompt cannot balloon the
    per-prompt hot path.

    SECURITY: lexemes are sanitized to ``^[a-z0-9]+$`` and passed as a bound
    ``%s::text[]`` array whose elements are cast to ``tsquery``, never
    concatenated into SQL.
    """
    safe = _sanitize_lexemes(lexemes)[:_MAX_DF_LEXEMES]
    if not safe:
        return []

    rows = conn.execute(_DF_BATCH_SQL, (safe,)).fetchall()
    if not rows:
        return []

    total = int(rows[0][2]) if rows[0][2] is not None else 0
    if total <= 0:
        return []

    scored: list[tuple[str, int]] = []
    for lexeme, df, _total in rows:
        df = int(df) if df is not None else 0
        if df <= 0:
            continue
        if (df / total) <= max_df_ratio:
            scored.append((str(lexeme), df))

    scored.sort(key=lambda item: item[1])  # rarest first
    return [lexeme for lexeme, _ in scored[: max(0, int(max_terms))]]


def _build_or_tsquery(terms: list[str]) -> Optional[str]:
    """Join sanitized terms into an OR tsquery *value* (bound, never inlined).

    Returns e.g. ``"igor | goal | share"`` which is cast with ``%s::tsquery``.
    Terms are re-sanitized defensively; returns None when nothing survives.
    """
    safe = _sanitize_lexemes(terms)
    if not safe:
        return None
    return " | ".join(safe)


# Per-schema SELECT that mirrors the ANN row shape for that schema, PLUS the
# row's true raw cosine (aliased ``similarity``). ``distance`` is derived in
# Python (1 - similarity) so the row is drop-in compatible with ANN rows, whose
# downstream code computes ``similarity = 1 - distance``.
#
# DIMENSIONS: the cast is the dimensionless ``%s::halfvec`` — exactly the ANN
# path convention (query.py). The column's own typmod enforces the configured
# embedding dimension, so this works at any dimension; a hardcoded
# ``halfvec(384)`` silently broke every lexical query after an embedding-model
# upgrade.
#
# USER ISOLATION: when a non-anonymous ``user`` is supplied the same
# ``metadata->>'user' = %s`` predicate the ANN path applies is added (mirrors
# _build_core_filter/_build_vault_filter), so the lexical channel cannot leak
# another user's rows.
#
# CHUNKED PARENTS (local): parents that have search windows in
# local.memory_chunks are EXCLUDED — exactly as the ANN path excludes them — so
# their distrusted mean-compatibility vector cannot re-enter ranking, they never
# monopolize the injection budget with full parent text, and the reranker/shadow
# scorer never sees the wrong text base.


def _obsidian_sql(*, user_scoped: bool) -> str:
    where = ["tsv @@ %s::tsquery"]
    if user_scoped:
        where.append("metadata->>'user' = %s")
    return (
        "SELECT id, document, metadata, parent_file, directory, vault_type, title, "
        "chunk_index, chunk_total, chunk_heading, importance_score, "
        "1 - (embedding <=> %s::halfvec) AS similarity, "
        "'obsidian' AS _schema "
        "FROM obsidian.documents "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY ts_rank_cd(tsv, %s::tsquery) DESC "
        "LIMIT %s"
    )


def _local_sql(*, user_scoped: bool) -> str:
    where = [
        "status = 'active'",
        "tsv @@ %s::tsquery",
        "NOT EXISTS (SELECT 1 FROM local.memory_chunks ch WHERE ch.parent_id = m.id)",
    ]
    if user_scoped:
        where.append("metadata->>'user' = %s")
    return (
        "SELECT id, document, metadata, category, scope, source, importance_score, "
        "retrieval_count, created_at, "
        "1 - (embedding <=> %s::halfvec) AS similarity, "
        "'local' AS _schema "
        "FROM local.memories AS m "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY ts_rank_cd(tsv, %s::tsquery) DESC "
        "LIMIT %s"
    )


def lexical_candidates(
    conn,
    terms: list[str],
    *,
    schema: str,
    limit: int,
    query_embedding: Any,
    user: Optional[str] = None,
) -> list[dict]:
    """Fetch lexical candidate rows for one schema (obsidian or local).

    OR-queries the informative ``terms`` over the schema's ``tsv`` column,
    ordered by ``ts_rank_cd`` DESC (the LIMIT tiebreak only), and returns rows
    in the SAME shape the ANN path yields for that schema — plus each row's
    true raw cosine to ``query_embedding``. ``distance`` is set so rows flow
    unchanged through the shared scoring/dedup/telemetry pipeline.

    When ``user`` is non-anonymous the per-user isolation predicate is applied,
    matching the ANN path. Chunked local parents are excluded (their chunks are
    searched via the ANN path instead).
    """
    if schema not in _SUPPORTED_SCHEMAS:
        return []
    limit = max(1, int(limit))
    user_scoped = bool(user) and user != "anonymous"
    sql = _obsidian_sql(user_scoped=user_scoped) if schema == "obsidian" else _local_sql(user_scoped=user_scoped)

    # PER-TERM SEQUENTIAL FILL, rarest term first. A single OR-query capped by
    # ts_rank_cd is IDF-blind: with terms like [igor, goal, main], hundreds of
    # goal/main-dense rows outrank the two rows containing the df=1 term and
    # flood the LIMIT — the exact candidate the channel exists to surface never
    # enters the pool. informative_terms returns terms rarest-first; giving
    # each term its own fetch (bounded by the remaining limit, deduped by id)
    # guarantees the rarest term's matches always claim seats, and by
    # definition rare terms have few rows, so early fetches are tiny.
    rows: list[dict] = []
    seen_ids: set = set()
    for term in _sanitize_lexemes(terms):
        remaining = limit - len(rows)
        if remaining <= 0:
            break
        params: list[Any] = [query_embedding, term]
        if user_scoped:
            params.append(user)
        params.extend([term, remaining])
        cur = conn.execute(sql, tuple(params))
        columns = [desc.name for desc in cur.description]
        for raw in cur.fetchall():
            row = dict(zip(columns, raw))
            if row["id"] in seen_ids:
                continue
            seen_ids.add(row["id"])
            rows.append(row)

    for row in rows:
        similarity = float(row.get("similarity") or 0.0)
        row["similarity"] = similarity
        # Downstream computes similarity = 1 - distance; keep it exact.
        row["distance"] = 1.0 - similarity
        row["_schema"] = schema
    return rows
