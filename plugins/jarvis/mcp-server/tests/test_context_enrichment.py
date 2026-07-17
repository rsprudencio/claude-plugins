"""Tests for context enrichment semantic search (UserPromptSubmit hook).

Tests cover:
- Prompt filtering (_should_skip_prompt)
- Prompt extraction from hook JSON (_extract_prompt)
- semantic_context() search function
- Output formatting (_format_memories)
- Context enrichment config (get_context_enrichment_config)
"""

import json
import sys
import os
import pytest

# Add hooks-handlers to path for importing context_enrichment module
HOOKS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "hooks-handlers"
)
sys.path.insert(0, HOOKS_DIR)

import context_enrichment as context_enrichment_module
from context_enrichment import _should_skip_prompt, _extract_prompt, _format_memories
from harness import (
    CLAUDE,
    CODEX,
    detect_harness,
    format_user_prompt_submit_output,
)


def _seed_docs(db, ids, documents, metadatas):
    """Seed documents into InMemoryDB with mock embeddings.

    Routes vault:: IDs to vault_rows, others to core_rows.
    """
    from tools.embedding import get_embedding_service

    emb = get_embedding_service()
    for doc_id, doc, meta in zip(ids, documents, metadatas):
        if doc_id.startswith("vault::"):
            m = dict(meta)
            db.upsert_vault(
                doc_id, doc, emb.encode(doc),
                parent_file=m.pop("parent_file", doc_id.replace("vault::", "")),
                directory=m.pop("directory", ""),
                vault_type=m.pop("vault_type", "document"),
                title=m.pop("title", ""),
                chunk_index=m.pop("chunk_index", 0),
                chunk_total=m.pop("chunk_total", 1),
                chunk_heading=m.pop("chunk_heading", ""),
                importance_score=m.pop("importance_score", 0.5),
                metadata=m,
            )
        else:
            db.upsert(doc_id, doc, emb.encode(doc), meta)


# --- Prompt Filtering Tests ---


class TestPromptFiltering:
    """Tests for _should_skip_prompt() — returns (should_skip, reason) tuple."""

    def test_short_prompt_skipped(self):
        """Prompts < 10 chars are skipped with 'short' reason."""
        skip, reason = _should_skip_prompt("yes")
        assert skip is True
        assert reason == "short"
        assert _should_skip_prompt("ok")[0] is True
        assert _should_skip_prompt("   hi   ")[0] is True

    def test_empty_prompt_skipped(self):
        """Empty/whitespace prompts are skipped."""
        assert _should_skip_prompt("")[0] is True
        assert _should_skip_prompt("   ")[0] is True

    def test_slash_command_skipped(self):
        """Slash commands are skipped with 'slash_cmd' reason."""
        skip, reason = _should_skip_prompt("/recall my goals")
        assert skip is True
        assert reason == "slash_cmd"
        assert _should_skip_prompt("/journal today")[0] is True
        assert _should_skip_prompt("/help")[0] is True

    def test_confirmation_skipped(self):
        """Known confirmation patterns are skipped with 'confirmation' reason."""
        # "go ahead" is long enough to not hit "short" — tests confirmation
        skip, reason = _should_skip_prompt("go ahead!!")
        assert skip is True
        assert reason == "confirmation"
        assert _should_skip_prompt("sounds good")[0] is True
        assert _should_skip_prompt("Got it.....")[0] is True
        assert _should_skip_prompt("OKAY!!!!!!")[0] is True

    def test_code_block_skipped(self):
        """Prompts starting with ``` are skipped with 'code_block' reason."""
        skip, reason = _should_skip_prompt("```python\nprint('hello')\n```")
        assert skip is True
        assert reason == "code_block"
        assert _should_skip_prompt("```\nsome code\n```")[0] is True

    def test_auto_extract_prompt_skipped(self):
        """Auto-extract Haiku prompts (via claude -p subprocess) are skipped."""
        skip, reason = _should_skip_prompt(
            "You are analyzing a conversation turn between a user and an AI assistant working on code.\n\n## User's Message\nhello"
        )
        assert skip is True
        assert reason == "auto_extract_prompt"
        # Also match without "working on code" suffix
        assert (
            _should_skip_prompt(
                "You are analyzing a conversation turn between a user and an AI assistant.\n\n## User's Message"
            )[0]
            is True
        )

    def test_auto_extract_prompt_matches_real_template(self):
        """The skip filter catches the ACTUAL extraction prompt template.

        This is a coupling guard: if EXTRACTION_PROMPT in extract_observation.py
        changes its prefix, this test breaks — forcing the filter in
        context_enrichment.py to be updated in sync.
        """
        from extract_observation import EXTRACTION_PROMPT

        # Format the real template with dummy values (same as build_turn_prompt)
        real_prompt = EXTRACTION_PROMPT.format(
            user_text="test user message",
            assistant_text="test assistant response",
            tool_names="Read, Edit",
            relevant_files="- /some/file.py",
            project_name="my-project",
            git_branch="main",
            token_usage="100 in, 50 out",
        )
        skip, reason = _should_skip_prompt(real_prompt)
        assert skip is True, (
            f"EXTRACTION_PROMPT changed but context_enrichment filter didn't catch it. "
            f"Update the auto_extract_prompt check in _should_skip_prompt()."
        )
        assert reason == "auto_extract_prompt"

    def test_substantive_prompt_not_skipped(self):
        """Normal questions and requests pass through with empty reason."""
        skip, reason = _should_skip_prompt("What should I focus on for my review?")
        assert skip is False
        assert reason == ""
        assert _should_skip_prompt("Help me plan the database migration")[0] is False
        assert _should_skip_prompt("What are my career goals for 2026?")[0] is False

    def test_long_confirmation_not_skipped(self):
        """Long messages that START with a confirmation word are NOT skipped."""
        assert (
            _should_skip_prompt("yes, and also can you check the deployment status")[0]
            is False
        )
        assert (
            _should_skip_prompt("sure, but first tell me about the auth system")[0]
            is False
        )
        assert (
            _should_skip_prompt("ok let me also ask about the database migration plan")[
                0
            ]
            is False
        )

    def test_borderline_length(self):
        """Prompts near the 10-char threshold."""
        assert _should_skip_prompt("12345678")[0] is True  # 8 chars
        assert _should_skip_prompt("123456789")[0] is True  # 9 chars
        assert _should_skip_prompt("1234567890")[0] is False  # exactly 10 chars


# --- Prompt Extraction Tests ---


class TestPromptExtraction:
    """Tests for _extract_prompt()."""

    def test_prompt_key(self):
        """Extracts from 'prompt' key."""
        data = json.dumps({"prompt": "What are my goals?"})
        assert _extract_prompt(data) == "What are my goals?"

    def test_user_prompt_key(self):
        """Extracts from 'user_prompt' key."""
        data = json.dumps({"user_prompt": "Tell me about work"})
        assert _extract_prompt(data) == "Tell me about work"

    def test_message_key(self):
        """Extracts from 'message' key."""
        data = json.dumps({"message": "Check my inbox"})
        assert _extract_prompt(data) == "Check my inbox"

    def test_nested_dict_prompt(self):
        """Handles nested dict with text/content keys."""
        data = json.dumps({"prompt": {"text": "nested prompt"}})
        assert _extract_prompt(data) == "nested prompt"

        data = json.dumps({"prompt": {"content": "nested content"}})
        assert _extract_prompt(data) == "nested content"

    def test_invalid_json(self):
        """Returns empty string on invalid JSON."""
        assert _extract_prompt("not json") == ""
        assert _extract_prompt("") == ""

    def test_missing_keys(self):
        """Returns empty string when no known keys exist."""
        data = json.dumps({"unknown_key": "value"})
        assert _extract_prompt(data) == ""

    def test_empty_prompt(self):
        """Returns empty string for empty prompt values."""
        data = json.dumps({"prompt": ""})
        assert _extract_prompt(data) == ""


# --- Harness Compatibility Tests ---


class TestHarnessCompatibility:
    """Tests the Claude/Codex UserPromptSubmit protocol boundary."""

    def test_detects_claude_from_legacy_plugin_root(self):
        assert detect_harness({"CLAUDE_PLUGIN_ROOT": "/plugins/jarvis"}) == CLAUDE

    def test_detects_codex_from_plugin_root(self):
        # Codex exposes both variables for compatibility. PLUGIN_ROOT wins.
        assert detect_harness(
            {
                "PLUGIN_ROOT": "/plugins/jarvis",
                "CLAUDE_PLUGIN_ROOT": "/plugins/jarvis",
            }
        ) == CODEX

    def test_defaults_to_claude_plain_output_without_hook_environment(self):
        """Direct/unit callers retain the historical plain-text behavior."""
        assert detect_harness({}) == CLAUDE

    def test_claude_output_is_plain_context(self):
        context = "<relevant-vault-memories>memory</relevant-vault-memories>"
        assert format_user_prompt_submit_output(context, CLAUDE) == context

    def test_codex_output_uses_hook_specific_additional_context(self):
        context = "<relevant-vault-memories>mémoire</relevant-vault-memories>"
        output = format_user_prompt_submit_output(context, CODEX)

        assert json.loads(output) == {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }
        # Keep human-readable Unicode in diagnostics and captured hook output.
        assert "mémoire" in output

    @pytest.mark.parametrize("harness", [CLAUDE, CODEX])
    def test_empty_context_is_silent_for_every_harness(self, harness):
        assert format_user_prompt_submit_output("", harness) == ""


class TestHookManifestPortability:
    """Keep every Jarvis hook reachable from both plugin harnesses."""

    def test_all_hook_commands_prefer_codex_root_with_claude_fallback(self):
        manifest_path = os.path.join(HOOKS_DIR, "..", "hooks", "hooks.json")
        with open(manifest_path) as manifest_file:
            hooks = json.load(manifest_file)["hooks"]

        commands = [
            hook["command"]
            for event_groups in hooks.values()
            for event_group in event_groups
            for hook in event_group["hooks"]
            if hook["type"] == "command"
        ]

        assert commands
        for command in commands:
            assert '${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}' in command
            assert 'bash "${PLUGIN_DIR}/' in command


# --- Output Formatting Tests ---


class TestOutputFormatting:
    """Tests for _format_memories()."""

    def test_empty_matches(self):
        """Returns empty string for no matches."""
        assert _format_memories([], 0) == ""

    def test_single_match(self):
        """Formats a single memory correctly."""
        matches = [
            {
                "id": "notes/goals.md",
                "relevance": 0.85,
                "type": "note",
                "content": "My career goals for 2026",
                "schema": "obsidian",
            }
        ]
        output = _format_memories(matches, 42.5)
        assert '<relevant-vault-memories count="1" query_ms="42.5">' in output
        assert 'id="notes/goals.md"' in output
        assert 'relevance="0.85"' in output
        assert 'type="note"' in output
        assert 'schema="obsidian"' in output
        assert "My career goals for 2026" in output
        assert "</relevant-vault-memories>" in output

    def test_multiple_matches(self):
        """Formats multiple memories."""
        matches = [
            {"id": "notes/a.md", "relevance": 0.9, "type": "note", "content": "A"},
            {
                "id": "notes/b.md",
                "relevance": 0.7,
                "type": "journal",
                "content": "B",
            },
        ]
        output = _format_memories(matches, 50.0)
        assert 'count="2"' in output
        assert "notes/a.md" in output
        assert "notes/b.md" in output

    def test_heading_attribute(self):
        """Includes heading attribute when present."""
        matches = [
            {
                "id": "notes/goals.md",
                "relevance": 0.8,
                "type": "note",
                "content": "Content",
                "heading": "Career Goals",
            }
        ]
        output = _format_memories(matches, 10.0)
        assert 'heading="Career Goals"' in output

    def test_no_heading_attribute(self):
        """Omits heading attribute when not present."""
        matches = [
            {
                "id": "notes/goals.md",
                "relevance": 0.8,
                "type": "note",
                "content": "Content",
            }
        ]
        output = _format_memories(matches, 10.0)
        assert "heading" not in output

    def test_xml_escaping(self):
        """Properly escapes XML special characters."""
        matches = [
            {
                "id": "notes/test.md",
                "relevance": 0.8,
                "type": "note",
                "content": "Use <b>bold</b> & 'quotes' in \"content\"",
            }
        ]
        output = _format_memories(matches, 10.0)
        assert "&lt;b&gt;bold&lt;/b&gt;" in output
        assert "&amp;" in output


class TestPromptSearchMain:
    """Tests for endpoint-backed main flow."""

    def test_unavailable_endpoint_emits_warning(self, monkeypatch, capsys):
        """When /hook/prompt-context is unavailable, warning XML is emitted."""
        monkeypatch.setattr(
            context_enrichment_module,
            "post_json",
            lambda *args, **kwargs: {
                "success": False,
                "data": None,
                "error": "connection refused",
            },
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["context_enrichment.py", "This is a substantive prompt for testing."],
        )

        with pytest.raises(SystemExit) as exc:
            context_enrichment_module.main()

        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert '<jarvis-warning type="memory-unavailable">' in output


# --- Semantic Context Tests ---


class TestSemanticContext:
    """Tests for semantic_context() query function."""

    def test_empty_collection(self, mock_config):
        """Returns empty matches for empty database."""
        from tools.query import semantic_context

        result = semantic_context("What are my goals?")
        assert result["matches"] == []
        assert result["total_searched"] == 0

    def test_returns_query_ms(self, mock_config):
        """Response includes query duration."""
        from tools.query import semantic_context

        result = semantic_context("test query")
        assert "query_ms" in result
        assert isinstance(result["query_ms"], (int, float))

    def test_threshold_filtering(self, mock_config):
        """Results below threshold are excluded."""
        from tools.query import semantic_context

        _seed_docs(
            mock_config.db,
            ids=["vault::notes/relevant.md", "vault::notes/unrelated.md"],
            documents=[
                "Career goals for 2026: leadership, technical depth",
                "Recipe for chocolate cake with frosting",
            ],
            metadatas=[
                {
                    "vault_type": "note",
                    "directory": "notes",
                    "title": "Goals",
                    "importance_score": 0.8,
                },
                {
                    "vault_type": "note",
                    "directory": "notes",
                    "title": "Recipes",
                    "importance_score": 0.3,
                },
            ],
        )

        # High threshold should return fewer/no results for off-topic query.
        # The gate compares RAW similarity (not the importance-boosted
        # relevance), so importance can never rescue an off-topic match.
        result = semantic_context("chocolate cake recipe", threshold=0.9)
        for match in result["matches"]:
            assert match["similarity"] >= 0.9

    def test_threshold_gates_on_raw_similarity_not_boosted_relevance(
        self, mock_config
    ):
        """Importance can never rescue an off-topic match past the gate, and
        can never sink an on-topic one below it — relevance (gate) and
        ranking (boost) are decoupled."""
        from unittest.mock import patch

        from tools.query import semantic_context

        # Seed one real doc so the non-empty-collection check passes
        _seed_docs(
            mock_config.db,
            ids=["vault::notes/filler.md"],
            documents=["Filler document"],
            metadatas=[{"vault_type": "note", "directory": "notes"}],
        )

        def _row(doc_id, distance, importance):
            parent = doc_id.replace("vault::", "").split("#")[0]
            return {
                "id": doc_id,
                "document": f"content for {parent}",
                "distance": distance,
                "_schema": "obsidian",
                "parent_file": parent,
                "directory": "notes",
                "vault_type": "note",
                "title": "T",
                "chunk_index": 0,
                "chunk_total": 1,
                "chunk_heading": "",
                "importance_score": importance,
                "metadata": {},
            }

        rows = [
            # similarity 0.7, importance 1.0 → boosted relevance 0.82
            _row("vault::notes/important-offtopic.md", 0.3, 1.0),
            # similarity 0.8, importance 0.0 → boosted relevance 0.68
            _row("vault::notes/plain-ontopic.md", 0.2, 0.0),
        ]
        with patch("tools.query._cross_schema_search", return_value=rows):
            result = semantic_context("unrelated query", threshold=0.75)

        ids = [m["id"] for m in result["matches"]]
        # Gate on boosted relevance would pass this (0.82 >= 0.75); the raw
        # similarity gate must filter it (0.7 < 0.75).
        assert "notes/important-offtopic.md" not in ids
        # Gate on boosted relevance would filter this (0.68 < 0.75); the raw
        # similarity gate must pass it (0.8 >= 0.75).
        assert "notes/plain-ontopic.md" in ids

    def test_budget_limits_results(self, mock_config):
        """Small budget limits number of returned matches."""
        from tools.query import semantic_context

        ids = [f"vault::notes/doc{i}.md" for i in range(10)]
        docs = [f"Document about career goals topic {i}" for i in range(10)]
        metas = [
            {
                "vault_type": "note",
                
                "directory": "notes",
                "title": f"Doc {i}",
                "importance_score": 0.5,
            }
            for i in range(10)
        ]
        _seed_docs(mock_config.db, ids, docs, metas)

        # Tiny budget (240 chars) should limit vault refs (~120 chars each)
        result_small = semantic_context("career goals", budget=240, threshold=0.0)
        # Large budget should return more
        result_large = semantic_context("career goals", budget=8000, threshold=0.0)
        assert len(result_small["matches"]) <= len(result_large["matches"])

    def test_max_results_caps_injection_after_ranking(self, mock_config):
        """The injection count is capped independently of a generous budget."""
        from tools.query import semantic_context

        ids = [f"vault::notes/capped{i}.md" for i in range(30)]
        docs = [f"Career leadership planning memory {i}" for i in range(30)]
        metas = [
            {
                "vault_type": "note",
                "directory": "notes",
                "title": f"Capped {i}",
                "importance_score": 0.5,
            }
            for i in range(30)
        ]
        _seed_docs(mock_config.db, ids, docs, metas)

        result = semantic_context(
            "career leadership planning",
            threshold=-1.0,
            budget=100_000,
            max_results=20,
        )

        assert len(result["matches"]) == 20

    def test_sensitive_dirs_excluded(self, mock_config):
        """Results from documents/ and people/ are never returned."""
        from tools.query import semantic_context

        _seed_docs(
            mock_config.db,
            ids=[
                "vault::notes/safe.md",
                "vault::documents/sensitive.md",
                "vault::people/contact.md",
            ],
            documents=[
                "Career goals and plans",
                "Career goals from sensitive document",
                "Career goals from people contact",
            ],
            metadatas=[
                {
                    "vault_type": "note",
                    "directory": "notes",
                    "title": "Safe",
                    "importance_score": 0.5,
                },
                {
                    "vault_type": "note",
                    "directory": "documents",
                    "title": "Sensitive",
                    "importance_score": 0.8,
                },
                {
                    "vault_type": "note",
                    "directory": "people",
                    "title": "Contact",
                    "importance_score": 0.8,
                },
            ],
        )

        result = semantic_context("career goals", threshold=0.0)
        ids = [m["id"] for m in result["matches"]]
        assert "notes/safe.md" in ids
        assert "documents/sensitive.md" not in ids
        assert "people/contact.md" not in ids
        assert result["skipped_sensitive"] >= 2

    def test_vault_shown_as_reference(self, mock_config):
        """Vault items use reference display mode (path only, no full content)."""
        from tools.query import semantic_context

        long_content = "Important career goal information. " * 100  # Very long
        _seed_docs(
            mock_config.db,
            ids=["vault::notes/long.md"],
            documents=[long_content],
            metadatas=[
                {
                    "vault_type": "note",
                    
                    "directory": "notes",
                    "title": "Long",
                    "importance_score": 0.8,
                },
            ],
        )

        result = semantic_context("career goals", threshold=0.0)
        if result["matches"]:
            match = result["matches"][0]
            assert match["display_mode"] == "reference"
            # Reference content is just the path, not the full document
            assert len(match["content"]) < 200

    def test_core_shown_in_full(self, mock_config):
        """Core memory items use full display mode with complete content."""
        from tools.query import semantic_context

        obs_content = (
            "User prefers kebab-case for all file naming conventions across the vault."
        )
        _seed_docs(
            mock_config.db,
            ids=["obs::1234567890"],
            documents=[obs_content],
            metadatas=[
                {"category": "observation", "importance_score": 0.8},
            ],
        )

        result = semantic_context("file naming conventions", threshold=0.0)
        if result["matches"]:
            match = next(
                (m for m in result["matches"] if m.get("display_mode") == "full"), None
            )
            if match:
                # Full content should be present, not truncated
                assert "kebab-case" in match["content"]

    def test_chunk_dedup(self, mock_config):
        """Only best chunk per parent file is returned."""
        from tools.query import semantic_context

        _seed_docs(
            mock_config.db,
            ids=["vault::notes/goals.md#chunk-0", "vault::notes/goals.md#chunk-1"],
            documents=[
                "Career goals for 2026 include leadership",
                "Other section about hobbies and travel",
            ],
            metadatas=[
                {
                    "vault_type": "note",
                    "directory": "notes",
                    "title": "Goals",
                    "importance_score": 0.8,
                    "parent_file": "notes/goals.md",
                    "chunk_heading": "Career",
                },
                {
                    "vault_type": "note",
                    "directory": "notes",
                    "title": "Goals",
                    "importance_score": 0.5,
                    "parent_file": "notes/goals.md",
                    "chunk_heading": "Hobbies",
                },
            ],
        )

        result = semantic_context("career goals", threshold=0.0)
        # Should only return 1 result (best chunk for goals.md)
        ids = [m["id"] for m in result["matches"]]
        assert ids.count("notes/goals.md") <= 1

    def test_budget_split_mixed_schemas(self, mock_config):
        """Budget splits 50/50 between core (full) and vault (reference) content."""
        from tools.query import semantic_context

        # Add 5 vault files (~120 chars each as references = 600 chars)
        vault_ids = [f"vault::notes/goal{i}.md" for i in range(5)]
        vault_docs = [
            f"Career goal document about leadership topic {i}" for i in range(5)
        ]
        vault_metas = [
            {
                "vault_type": "note",
                "directory": "notes",
                "title": f"Goal {i}",
                "importance_score": 0.8,
                "parent_file": f"notes/goal{i}.md",
            }
            for i in range(5)
        ]

        # Add 3 core observations (~300 chars each = 900 chars)
        obs_ids = [f"obs::{1770000000000 + i}" for i in range(3)]
        obs_docs = [
            f"User career preference observation number {i}: "
            + "detailed information about work habits and goals. " * 4
            for i in range(3)
        ]
        obs_metas = [
            {"category": "observation", "importance_score": 0.8}
            for _ in range(3)
        ]

        _seed_docs(
            mock_config.db,
            vault_ids + obs_ids,
            vault_docs + obs_docs,
            vault_metas + obs_metas,
        )

        # Budget=2000: half=1000 per side
        # Vault side: 1000/120 ≈ 8 refs (enough for all 5)
        # Core side: 1000/~300 ≈ 3 obs (enough for all 3)
        # Raw cosine spans -1..1; disable the quality gate so this test isolates
        # budget allocation across schemas.
        result = semantic_context("career goals leadership", budget=2000, threshold=-1.0)
        matches = result["matches"]

        vault_matches = [m for m in matches if m.get("display_mode") == "reference"]
        core_matches = [m for m in matches if m.get("display_mode") == "full"]

        # Both schemas should be represented
        assert len(vault_matches) > 0, "Expected vault references in results"
        assert len(core_matches) > 0, "Expected core full content in results"

        # Budget tracking should report usage for both halves
        budget_used = result.get("budget_used", {})
        assert budget_used.get("local", 0) > 0, "Expected local budget usage"
        assert budget_used.get("vault", 0) > 0, "Expected vault budget usage"
        assert budget_used["local"] + budget_used["vault"] + budget_used.get("remote", 0) <= 2000

    def test_budget_overflow_from_empty_half(self, mock_config):
        """Unused budget from one half overflows to the other."""
        from tools.query import semantic_context

        # Add ONLY vault files (no core) — all budget should be available for vault
        vault_ids = [f"vault::notes/item{i}.md" for i in range(50)]
        vault_docs = [
            f"Important career topic and leadership content {i}" for i in range(50)
        ]
        vault_metas = [
            {
                "vault_type": "note",
                
                "directory": "notes",
                "title": f"Item {i}",
                "importance_score": 0.8,
                "parent_file": f"notes/item{i}.md",
            }
            for i in range(50)
        ]
        _seed_docs(mock_config.db, vault_ids, vault_docs, vault_metas)

        # Budget=1200: half=600. Vault refs cost ~120 each.
        # Without overflow: 600/120 = 5 vault refs
        # With overflow: (600+600)/120 = 10 vault refs
        result = semantic_context("career leadership", budget=1200, threshold=0.0)
        vault_matches = [
            m for m in result["matches"] if m.get("display_mode") == "reference"
        ]

        # Should get more than 5 (the non-overflow limit) because core half is unused
        assert (
            len(vault_matches) > 5
        ), f"Expected >5 vault refs with overflow, got {len(vault_matches)}"


# --- Per-Prompt Config Tests ---


class TestContextEnrichmentConfig:
    """Tests for get_context_enrichment_config()."""

    def test_defaults_when_no_config(self, mock_config):
        """Missing memory config uses defaults."""
        from tools.config import get_context_enrichment_config

        config = get_context_enrichment_config()
        assert config["enabled"] is True
        assert config["threshold"] == 0.876
        assert config["budget"] == 8000
        assert config["max_results"] == 20
        assert config["passive_retrieval_increment"] == 0.01

    def test_disabled(self, mock_config):
        """Config can disable context enrichment."""
        import tools.config as config_module

        config_module.clear_config_cache()

        config_data = json.loads(mock_config.path.read_text())
        config_data.setdefault("memory", {})["context_enrichment"] = {"enabled": False}
        mock_config.path.write_text(json.dumps(config_data))
        config_module.clear_config_cache()

        from tools.config import get_context_enrichment_config

        config = get_context_enrichment_config()
        assert config["enabled"] is False

    def test_custom_threshold(self, mock_config):
        """Custom threshold overrides default."""
        import tools.config as config_module

        config_module.clear_config_cache()

        config_data = json.loads(mock_config.path.read_text())
        config_data.setdefault("memory", {})["context_enrichment"] = {"threshold": 0.7}
        mock_config.path.write_text(json.dumps(config_data))
        config_module.clear_config_cache()

        from tools.config import get_context_enrichment_config

        config = get_context_enrichment_config()
        assert config["threshold"] == 0.7
        # Other defaults preserved
        assert config["enabled"] is True
        assert config["budget"] == 8000

    def test_custom_budget(self, mock_config):
        """Custom budget overrides default."""
        import tools.config as config_module

        config_module.clear_config_cache()

        config_data = json.loads(mock_config.path.read_text())
        config_data.setdefault("memory", {})["context_enrichment"] = {"budget": 12000}
        mock_config.path.write_text(json.dumps(config_data))
        config_module.clear_config_cache()

        from tools.config import get_context_enrichment_config

        config = get_context_enrichment_config()
        assert config["budget"] == 12000


# --- Dedup Integration Tests ---


class TestDedupIntegration:
    """Tests for dedup wire-up in context enrichment main flow."""

    @pytest.fixture(autouse=True)
    def _patch_state_dir(self, tmp_path):
        """Redirect injection state to temp dir."""
        from unittest.mock import patch
        with patch("precompact_dedup.STATE_DIR", tmp_path), \
             patch("context_enrichment.write_injection_state") as mock_write, \
             patch("context_enrichment._write_telemetry") as mock_telemetry, \
             patch("context_enrichment.filter_already_injected", side_effect=lambda m, s: m) as mock_filter:
            self.mock_write = mock_write
            self.mock_telemetry = mock_telemetry
            self.mock_filter = mock_filter
            self.tmp_path = tmp_path
            yield

    def _make_success_response(self, matches):
        """Build a mock /hook/prompt-context success response."""
        return {
            "success": True,
            "data": {
                "enabled": True,
                "debug": False,
                "matches": matches,
                "query_ms": 10,
                "budget_used": {"local": 100, "vault": 200, "remote": 0},
                "todoist_prompt_alerts": {"enabled": False, "max_per_category": 3},
            },
        }

    def test_matches_filtered_when_injection_state_exists(self, monkeypatch, capsys):
        """filter_already_injected is called with matches and session_id."""
        matches = [
            {"content": "memory one", "id": "a.md", "relevance": 0.9, "type": "note"},
        ]
        monkeypatch.setattr(
            context_enrichment_module, "post_json",
            lambda *a, **kw: self._make_success_response(matches),
        )
        hook_input = json.dumps({"prompt": "What are my goals?", "session_id": "sess-42"})
        monkeypatch.setattr(sys, "argv", ["context_enrichment.py", "--hook"])
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(hook_input))

        with pytest.raises(SystemExit):
            context_enrichment_module.main()

        self.mock_filter.assert_called_once()
        call_args = self.mock_filter.call_args
        assert call_args[0][0] == matches
        assert call_args[0][1] == "sess-42"

    def test_injection_state_written_after_output(self, monkeypatch, capsys):
        """write_injection_state is called with correct hashes after injection."""
        from precompact_dedup import compute_content_hash

        matches = [
            {"content": "memory one", "id": "a.md", "relevance": 0.9, "type": "note"},
            {"content": "memory two", "id": "b.md", "relevance": 0.8, "type": "note"},
        ]
        monkeypatch.setattr(
            context_enrichment_module, "post_json",
            lambda *a, **kw: self._make_success_response(matches),
        )
        hook_input = json.dumps({"prompt": "What are my goals?", "session_id": "sess-99"})
        monkeypatch.setattr(sys, "argv", ["context_enrichment.py", "--hook"])
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(hook_input))

        with pytest.raises(SystemExit):
            context_enrichment_module.main()

        output = capsys.readouterr().out
        assert "relevant-vault-memories" in output

        self.mock_write.assert_called_once()
        call_args = self.mock_write.call_args
        assert call_args[0][0] == "sess-99"
        expected_hashes = [compute_content_hash(m["content"]) for m in matches]
        assert call_args[0][1] == expected_hashes
        assert call_args[0][2] == ["a.md", "b.md"]

    @pytest.mark.parametrize("harness", [CLAUDE, CODEX])
    def test_hook_output_matches_active_harness_protocol(
        self, harness, monkeypatch, capsys
    ):
        """The same retrieval result gets only a harness-specific envelope."""
        matches = [
            {
                "content": "Prefer focused memory injection.",
                "id": "decision/retrieval.md",
                "relevance": 0.91,
                "type": "decision",
            },
        ]
        calls = []

        def fake_post(path, payload, **kwargs):
            calls.append((path, payload, kwargs))
            return self._make_success_response(matches)

        monkeypatch.setattr(context_enrichment_module, "post_json", fake_post)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugins/jarvis")
        if harness == CODEX:
            monkeypatch.setenv("PLUGIN_ROOT", "/plugins/jarvis")
            hook_input = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "codex-session",
                "turn_id": "turn-17",
                "cwd": "/workspace",
                "prompt": "How should memory retrieval remain focused?",
            }
        else:
            monkeypatch.delenv("PLUGIN_ROOT", raising=False)
            hook_input = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "claude-session",
                "transcript_path": "/tmp/transcript.jsonl",
                "cwd": "/workspace",
                "prompt": "How should memory retrieval remain focused?",
            }

        monkeypatch.setattr(sys, "argv", ["context_enrichment.py", "--hook"])
        monkeypatch.setattr(
            sys, "stdin", __import__("io").StringIO(json.dumps(hook_input))
        )

        with pytest.raises(SystemExit) as exc:
            context_enrichment_module.main()

        assert exc.value.code == 0
        stdout = capsys.readouterr().out.strip()
        if harness == CODEX:
            response = json.loads(stdout)
            assert set(response) == {"hookSpecificOutput"}
            hook_output = response["hookSpecificOutput"]
            assert hook_output["hookEventName"] == "UserPromptSubmit"
            injected = hook_output["additionalContext"]
        else:
            assert stdout.startswith("<relevant-vault-memories")
            injected = stdout

        assert "Prefer focused memory injection." in injected
        assert calls == [
            (
                "/hook/prompt-context",
                {"prompt": "How should memory retrieval remain focused?"},
                {"timeout_seconds": 2.5},
            )
        ]
        self.mock_filter.assert_called_once_with(matches, hook_input["session_id"])
        self.mock_write.assert_called_once()
        self.mock_telemetry.assert_called_once()

    @pytest.mark.parametrize("harness", [CLAUDE, CODEX])
    def test_no_matches_emit_no_hook_output(self, harness, monkeypatch, capsys):
        """A valid zero-result search stays completely silent."""
        monkeypatch.setattr(
            context_enrichment_module,
            "post_json",
            lambda *a, **kw: self._make_success_response([]),
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugins/jarvis")
        if harness == CODEX:
            monkeypatch.setenv("PLUGIN_ROOT", "/plugins/jarvis")
        else:
            monkeypatch.delenv("PLUGIN_ROOT", raising=False)
        monkeypatch.setattr(sys, "argv", ["context_enrichment.py", "--hook"])
        monkeypatch.setattr(
            sys,
            "stdin",
            __import__("io").StringIO(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "empty-session",
                        "prompt": "A substantive prompt with no matching memory",
                    }
                )
            ),
        )

        with pytest.raises(SystemExit) as exc:
            context_enrichment_module.main()

        assert exc.value.code == 0
        assert capsys.readouterr().out == ""
        self.mock_write.assert_not_called()
        self.mock_telemetry.assert_not_called()

    def test_no_filter_when_no_session_id(self, monkeypatch, capsys):
        """Direct mode (no --hook) passes empty session_id to filter."""
        matches = [
            {"content": "memory one", "id": "a.md", "relevance": 0.9, "type": "note"},
        ]
        monkeypatch.setattr(
            context_enrichment_module, "post_json",
            lambda *a, **kw: self._make_success_response(matches),
        )
        monkeypatch.setattr(
            sys, "argv",
            ["context_enrichment.py", "What are my goals for the year?"],
        )

        with pytest.raises(SystemExit):
            context_enrichment_module.main()

        self.mock_filter.assert_called_once()
        call_args = self.mock_filter.call_args
        assert call_args[0][1] == ""  # No session_id in direct mode


class TestDecayDisabledScoring:
    """When memory.decay.enabled is False, core-like rows route through the
    unified formula with RAW base importance — same scale as vault chunks."""

    def test_decay_disabled_memory_scores_with_raw_importance(self, mock_config):
        from unittest.mock import patch

        from tools.query import semantic_context
        from tools.ranking import compute_unified_score

        mock_config.set(memory={"decay": {"enabled": False}})

        # Seed one real doc so the non-empty-collection check passes
        _seed_docs(
            mock_config.db,
            ids=["vault::notes/filler2.md"],
            documents=["Filler document two"],
            metadatas=[{"vault_type": "note", "directory": "notes"}],
        )

        row = {
            "id": "obs::1738850000000",
            "document": "memory content",
            "distance": 0.08,  # raw cosine similarity 0.92
            "_schema": "local",
            "category": "observation",
            "scope": "global",
            "source": "auto-extract",
            "importance_score": 0.9,
            "metadata": {},
            "created_at": "2026-01-01T00:00:00Z",
        }
        with patch("tools.query._cross_schema_search", return_value=[row]):
            result = semantic_context("decay disabled scoring", threshold=0.0)

        assert len(result["matches"]) == 1
        match = result["matches"][0]
        similarity = 1.0 - 0.08
        expected = compute_unified_score(similarity, 0.9)
        assert match["relevance"] == round(expected, 3)
        assert match["similarity"] == round(similarity, 3)
