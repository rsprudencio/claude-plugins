"""Semantic search e2e tests — real PostgreSQL + pgvector.

Verifies embedding <=> halfvec cosine distance, cross-schema UNION ALL,
and batch UPDATE retrieval count increment against real SQL.
"""

import os

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("E2E_POSTGRES_URL"),
        reason="E2E_POSTGRES_URL not set",
    ),
]


def test_query_finds_related_content(e2e_config):
    """embedding <=> %s::halfvec ORDER BY distance — cosine similarity search."""
    from tools.content import content_write
    from tools.query import query_vault

    # Seed some content in local.memories
    content_write(
        content="Python is a great language for data science and machine learning",
        content_type="observation",
        importance_score=0.8,
        skip_secret_scan=True,
    )
    content_write(
        content="The user's favorite coffee shop is on Main Street",
        content_type="observation",
        importance_score=0.5,
        skip_secret_scan=True,
    )

    result = query_vault("programming languages and AI", n_results=5)
    assert result["success"] is True
    assert len(result["results"]) >= 1


def test_search_increments_retrieval_count(e2e_config):
    """Batch UPDATE local.memories SET retrieval_count = retrieval_count + %s."""
    from tools.content import content_write, content_read
    from tools.query import query_vault

    result = content_write(
        content="Unique searchable content for retrieval count testing xyz123",
        content_type="observation",
        importance_score=0.9,
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    # Initial retrieval_count should be 0
    # content_read increments by 1, so now it's 1
    initial = content_read(doc_id)
    count_after_read = initial["retrieval_count"]

    # Search should trigger _increment_retrieval_counts
    query_vault("retrieval count testing xyz123", n_results=5)

    # Read again to check the count increased
    final = content_read(doc_id)
    count_final = final["retrieval_count"]
    # count_after_read was 1, then query_vault added 1, then this read added 1
    assert count_final > count_after_read


def test_cross_schema_search(e2e_config):
    """UNION ALL search across local.memories and obsidian.documents."""
    from tools.content import content_write
    from tools.memory import index_file
    from tools.query import query_vault

    # Seed content in local.memories
    content_write(
        content="Authentication uses OAuth 2.0 with PKCE flow",
        content_type="learning",
        importance_score=0.8,
        skip_secret_scan=True,
    )

    # Seed content in obsidian.documents via indexing
    vault_dir = e2e_config["vault_dir"]
    notes_dir = vault_dir / "notes"
    auth_file = notes_dir / "auth-guide.md"
    auth_file.write_text("# Auth Guide\n\nOAuth 2.0 with PKCE for security.")
    index_file(str(auth_file))

    # Search should find results from both schemas
    result = query_vault("OAuth authentication", n_results=10)
    assert result["success"] is True
    assert len(result["results"]) >= 2

    # Should have results from both schemas
    schemas = {r.get("schema") for r in result["results"]}
    assert "local" in schemas or "obsidian" in schemas


def test_vault_document_search(e2e_config):
    """Vault documents indexed into obsidian.documents are searchable."""
    from tools.memory import index_file
    from tools.query import query_vault

    vault_dir = e2e_config["vault_dir"]
    notes_dir = vault_dir / "notes"
    test_file = notes_dir / "python-tips.md"
    test_file.write_text(
        "# Python Tips\n\nUse list comprehensions for cleaner code.\n"
        "Generator expressions save memory for large datasets."
    )
    index_file(str(test_file))

    result = query_vault("Python list comprehension", n_results=5)
    assert result["success"] is True
    assert len(result["results"]) >= 1


def test_self_match_memory_ranks_first(e2e_config):
    """Defect #6 regression (Layer 4 unified scoring): a perfect-match memory
    must rank #1, even against high-similarity, high-importance, freshly
    indexed vault chunks.

    Under the pre-v3.4.0 formulas this fails: the memory's blended score
    capped at 0.7*1.0 + 0.3*0.8 = 0.94 while the vault chunk's clamped score
    pinned at min(1.0, 0.95 + 0.096 + 0.08) = 1.0 — in production a memory
    queried with its own embedding ranked #2534 of 2,864.
    """
    import math

    from tools.content import content_write
    from tools.embedding import get_embedding_service
    from tools.memory import index_file
    from tools.query import query_vault
    from tools.schema import execute_query

    # Text chosen to trigger no query expansion (expansion would change the
    # query embedding and break the exact self-match).
    memory_text = (
        "Quantum chromodynamics lattice simulation favors staggered fermions"
    )
    write_result = content_write(
        content=memory_text,
        content_type="learning",
        importance_score=0.8,
        skip_secret_scan=True,
    )
    assert write_result["success"] is True
    memory_id = write_result["id"]

    # Vault decoy: index a real file, then plant an embedding at cosine ~0.9
    # to the query vector — a strong-but-imperfect match with high importance.
    vault_dir = e2e_config["vault_dir"]
    decoy_file = vault_dir / "notes" / "decoy.md"
    decoy_file.write_text(
        "---\ntype: note\nimportance: high\n---\n"
        "# Decoy\n\nStrong but imperfect match content."
    )
    # Relative path — index_file stores it verbatim as parent_file
    index_result = index_file("notes/decoy.md")
    assert index_result["success"] is True

    v = get_embedding_service().encode(memory_text)
    # Unit vector orthogonal to v (Gram-Schmidt on e0)
    e0 = [1.0] + [0.0] * (len(v) - 1)
    dot = sum(a * b for a, b in zip(e0, v))
    u = [a - dot * b for a, b in zip(e0, v)]
    norm = math.sqrt(sum(x * x for x in u))
    u = [x / norm for x in u]
    # cos(v, w) = 0.9 exactly (before halfvec rounding)
    w = [0.9 * a + math.sqrt(1 - 0.81) * b for a, b in zip(v, u)]

    updated = execute_query(
        "UPDATE obsidian.documents SET embedding = %s::halfvec, "
        "importance_score = 0.9 WHERE parent_file = %s RETURNING id",
        ("[" + ",".join(f"{x:.8f}" for x in w) + "]", "notes/decoy.md"),
    )
    assert len(updated) == 1

    result = query_vault(memory_text, n_results=5)
    assert result["success"] is True
    assert len(result["results"]) >= 2

    top = result["results"][0]
    assert top["id"] == memory_id, (
        f"perfect-match memory must rank #1, got {top['id']} "
        f"(relevance {top['relevance']}, similarity {top.get('similarity')})"
    )
    # The decoy must still be present and strong — otherwise this test isn't
    # exercising the cross-schema comparison at all.
    decoy_hits = [r for r in result["results"] if r["path"] == "notes/decoy.md"]
    assert decoy_hits, "decoy chunk must appear in results"
    assert decoy_hits[0]["similarity"] >= 0.85
