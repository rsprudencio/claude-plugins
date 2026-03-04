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
                "source": "notes/goals.md",
                "relevance": 0.85,
                "type": "note",
                "content": "My career goals for 2026",
            }
        ]
        output = _format_memories(matches, 42.5)
        assert '<relevant-vault-memories count="1" query_ms="42.5">' in output
        assert 'source="notes/goals.md"' in output
        assert 'relevance="0.85"' in output
        assert 'type="note"' in output
        assert "My career goals for 2026" in output
        assert "</relevant-vault-memories>" in output

    def test_multiple_matches(self):
        """Formats multiple memories."""
        matches = [
            {"source": "notes/a.md", "relevance": 0.9, "type": "note", "content": "A"},
            {
                "source": "notes/b.md",
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
                "source": "notes/goals.md",
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
                "source": "notes/goals.md",
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
                "source": "notes/test.md",
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

        # High threshold should return fewer/no results for off-topic query
        result = semantic_context("chocolate cake recipe", threshold=0.9)
        # With very high threshold, only extremely relevant results pass
        for match in result["matches"]:
            assert match["relevance"] >= 0.9

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
        sources = [m["source"] for m in result["matches"]]
        assert "notes/safe.md" in sources
        assert "documents/sensitive.md" not in sources
        assert "people/contact.md" not in sources
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
        sources = [m["source"] for m in result["matches"]]
        assert sources.count("notes/goals.md") <= 1

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
        result = semantic_context("career goals leadership", budget=2000, threshold=0.0)
        matches = result["matches"]

        vault_matches = [m for m in matches if m.get("display_mode") == "reference"]
        core_matches = [m for m in matches if m.get("display_mode") == "full"]

        # Both schemas should be represented
        assert len(vault_matches) > 0, "Expected vault references in results"
        assert len(core_matches) > 0, "Expected core full content in results"

        # Budget tracking should report usage for both halves
        budget_used = result.get("budget_used", {})
        assert budget_used.get("core", 0) > 0, "Expected core budget usage"
        assert budget_used.get("vault", 0) > 0, "Expected vault budget usage"
        assert budget_used["core"] + budget_used["vault"] <= 2000

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
        assert config["threshold"] == 0.5
        assert config["budget"] == 8000
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
             patch("context_enrichment.filter_already_injected", side_effect=lambda m, s: m) as mock_filter:
            self.mock_write = mock_write
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
                "budget_used": {"core": 100, "vault": 200},
                "todoist_prompt_alerts": {"enabled": False, "max_per_category": 3},
            },
        }

    def test_matches_filtered_when_injection_state_exists(self, monkeypatch, capsys):
        """filter_already_injected is called with matches and session_id."""
        matches = [
            {"content": "memory one", "source": "a.md", "relevance": 0.9, "type": "note"},
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
            {"content": "memory one", "source": "a.md", "relevance": 0.9, "type": "note"},
            {"content": "memory two", "source": "b.md", "relevance": 0.8, "type": "note"},
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

    def test_no_filter_when_no_session_id(self, monkeypatch, capsys):
        """Direct mode (no --hook) passes empty session_id to filter."""
        matches = [
            {"content": "memory one", "source": "a.md", "relevance": 0.9, "type": "note"},
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
