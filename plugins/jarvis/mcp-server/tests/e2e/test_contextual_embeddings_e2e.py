"""E2E: contextual chunk augmentation proves the retrieval win — real PostgreSQL.

Indexes a file whose distinctive term ("mandate", "igor") appears ONLY in its
title/filename, never in the body chunks. A query using that term must retrieve
the file's chunk BECAUSE the embedded text carries a document-context prefix.
The same query with augmentation OFF must not surface the chunk with the same
strength — that contrast is the whole point of the feature.

Uses a deterministic bag-of-words embedding service so token overlap maps to
real pgvector cosine similarity (the default hash-based MockEmbeddingService has
no semantic structure and can't demonstrate the effect).
"""

import math
import os
import re

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("E2E_POSTGRES_URL"),
        reason="E2E_POSTGRES_URL not set",
    ),
]


class _VocabEmbeddingService:
    """Deterministic bag-of-words embeddings: cosine ≈ token overlap.

    Each distinct token is assigned its own dimension on first sight (shared
    instance across index + query), so — unlike hashing into 384 buckets —
    there are NO collisions between the query terms and unrelated body tokens.
    Vectors are L2-normalized: texts that share tokens have positive cosine
    similarity; texts with no shared tokens are exactly orthogonal.
    """

    def __init__(self, dimensions: int = 384):
        self._dimensions = dimensions
        self._vocab: dict[str, int] = {}

    def _dim(self, tok: str) -> int:
        if tok not in self._vocab:
            # Last dimension is a shared overflow bucket if we ever exceed the
            # vector width (the test vocabulary stays well under 384).
            self._vocab[tok] = min(len(self._vocab), self._dimensions - 1)
        return self._vocab[tok]

    def _vec(self, text: str):
        vec = [0.0] * self._dimensions
        for tok in re.findall(r"[a-z0-9]+", (text or "").lower()):
            vec[self._dim(tok)] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm > 0 else vec

    def encode(self, text: str):
        return self._vec(text)

    def encode_batch(self, texts, batch_size: int = 64):
        return [self._vec(t) for t in texts]

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return "vocab-model"

    @property
    def backend(self) -> str:
        return "mock"

    @property
    def is_loaded(self) -> bool:
        return True


# The distinctive terms ("igor", "mandate") appear ONLY in the filename (→ the
# derived title, since there is no H1). The body is pure H2 sections with none
# of those terms, and no H1 preamble chunk — so a body-only embedding shares
# nothing with the query. >200 chars per section so the file genuinely chunks.
_MANDATE_DOC = (
    "---\ntype: note\n---\n"
    "## Original Request\n\n"
    "Improve scanning coverage across every cloud account and container host, "
    "tighten remediation timelines with clearly assigned ownership for each "
    "engineering team, and establish a weekly triage cadence for newly "
    "discovered exposures across the whole fleet.\n\n"
    "## Scope of Work\n\n"
    "Stand up executive dashboards for asset owners, define service level tiers "
    "by severity band, and run structured quarterly reviews together with the "
    "platform reliability and security operations groups across all regions and "
    "business units."
)

_DECOY_DOC = (
    "# Coffee brewing notes\n\n"
    "## Method\n\n"
    + ("Grind size and water temperature shape espresso extraction quality. " * 5)
    + "\n\n"
    "## Beans\n\n"
    + ("Single origin beans from various regions vary in tasting notes. " * 5)
)

_TARGET = "notes/igor-mandate.md"
_QUERY = "igor mandate"


def _install_bow(monkeypatch):
    import tools.embedding as embedding_module

    bow = _VocabEmbeddingService(384)
    monkeypatch.setattr(embedding_module, "get_embedding_service", lambda: bow)
    monkeypatch.setattr(embedding_module, "_service", bow)


def _target_similarity(result) -> float:
    for entry in result.get("results", []):
        if entry.get("path") == _TARGET:
            return float(entry.get("similarity", 0.0))
    return 0.0


def test_title_only_term_retrieves_chunk_with_augmentation(e2e_config, monkeypatch):
    _install_bow(monkeypatch)

    notes = e2e_config["vault_dir"] / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "igor-mandate.md").write_text(_MANDATE_DOC)
    (notes / "coffee.md").write_text(_DECOY_DOC)

    from tools.memory import index_vault
    from tools.query import query_vault

    # ── Augmentation ON (default) ─────────────────────────────────────
    indexed = index_vault(force=True)
    assert indexed["success"] is True
    # The mandate file must genuinely chunk (multi-fragment), else there is
    # nothing to augment.
    assert indexed["chunks_total"] > indexed["files_indexed"]

    result_on = query_vault(_QUERY, n_results=5)
    assert result_on["success"] is True
    sim_on = _target_similarity(result_on)
    # The chunk is retrieved and its body shares no tokens with the query — the
    # only overlap comes from the injected title/path/heading prefix.
    assert sim_on > 0.2, f"expected the augmented chunk to surface, got {sim_on}"
    assert result_on["results"][0]["path"] == _TARGET, "target should rank #1"

    # ── Augmentation OFF (rollback switch) ────────────────────────────
    monkeypatch.setattr(
        "tools.memory.get_contextual_embeddings_enabled", lambda: False
    )
    reindexed = index_vault(force=True)
    assert reindexed["success"] is True

    result_off = query_vault(_QUERY, n_results=5)
    assert result_off["success"] is True
    sim_off = _target_similarity(result_off)

    # Without the prefix the body has zero overlap with "igor mandate", so the
    # chunk's similarity collapses — this is the measurable win.
    assert sim_off < 0.05, f"expected near-zero similarity without prefix, got {sim_off}"
    assert sim_on > sim_off + 0.2
