"""Tests for per-prompt semantic search (UserPromptSubmit hook).

Tests cover:
- Prompt filtering (_should_skip_prompt)
- Prompt extraction from hook JSON (_extract_prompt)
- semantic_context() search function
- Output formatting (_format_memories)
- Per-prompt config (get_per_prompt_config)
"""
import json
import sys
import os
import pytest

# Add hooks-handlers to path for importing prompt_search module
HOOKS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "hooks-handlers")
sys.path.insert(0, HOOKS_DIR)

from prompt_search import _should_skip_prompt, _extract_prompt, _format_memories


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
        assert _should_skip_prompt(
            "You are analyzing a conversation turn between a user and an AI assistant.\n\n## User's Message"
        )[0] is True

    def test_auto_extract_prompt_matches_real_template(self):
        """The skip filter catches the ACTUAL extraction prompt template.

        This is a coupling guard: if EXTRACTION_PROMPT in extract_observation.py
        changes its prefix, this test breaks — forcing the filter in
        prompt_search.py to be updated in sync.
        """
        from extract_observation import EXTRACTION_PROMPT

        # Format the real template with dummy values (same as build_turn_prompt)
        real_prompt = EXTRACTION_PROMPT.format(
            user_text="test user message",
            assistant_text="test assistant response",
            tool_names="Read, Edit",
            relevant_files="- /some/file.py",
            project_dir="my-project",
            git_branch="main",
            token_usage="100 in, 50 out",
        )
        skip, reason = _should_skip_prompt(real_prompt)
        assert skip is True, (
            f"EXTRACTION_PROMPT changed but prompt_search filter didn't catch it. "
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
        assert _should_skip_prompt("yes, and also can you check the deployment status")[0] is False
        assert _should_skip_prompt("sure, but first tell me about the auth system")[0] is False
        assert _should_skip_prompt("ok let me also ask about the database migration plan")[0] is False

    def test_borderline_length(self):
        """Prompts near the 10-char threshold."""
        assert _should_skip_prompt("12345678")[0] is True    # 8 chars
        assert _should_skip_prompt("123456789")[0] is True    # 9 chars
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
        matches = [{
            "source": "notes/goals.md",
            "relevance": 0.85,
            "type": "note",
            "content": "My career goals for 2026",
        }]
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
            {"source": "notes/b.md", "relevance": 0.7, "type": "journal", "content": "B"},
        ]
        output = _format_memories(matches, 50.0)
        assert 'count="2"' in output
        assert "notes/a.md" in output
        assert "notes/b.md" in output

    def test_heading_attribute(self):
        """Includes heading attribute when present."""
        matches = [{
            "source": "notes/goals.md",
            "relevance": 0.8,
            "type": "note",
            "content": "Content",
            "heading": "Career Goals",
        }]
        output = _format_memories(matches, 10.0)
        assert 'heading="Career Goals"' in output

    def test_no_heading_attribute(self):
        """Omits heading attribute when not present."""
        matches = [{
            "source": "notes/goals.md",
            "relevance": 0.8,
            "type": "note",
            "content": "Content",
        }]
        output = _format_memories(matches, 10.0)
        assert "heading" not in output

    def test_xml_escaping(self):
        """Properly escapes XML special characters."""
        matches = [{
            "source": "notes/test.md",
            "relevance": 0.8,
            "type": "note",
            "content": "Use <b>bold</b> & 'quotes' in \"content\"",
        }]
        output = _format_memories(matches, 10.0)
        assert "&lt;b&gt;bold&lt;/b&gt;" in output
        assert "&amp;" in output


# --- Semantic Context Tests ---

class TestSemanticContext:
    """Tests for semantic_context() query function."""

    def test_empty_collection(self, mock_config):
        """Returns empty matches for empty ChromaDB collection."""
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
        from tools.memory import _get_collection
        from tools.query import semantic_context

        collection = _get_collection()
        # Add documents (one very relevant, one less so)
        collection.add(
            ids=["vault::notes/relevant.md", "vault::notes/unrelated.md"],
            documents=[
                "Career goals for 2026: leadership, technical depth",
                "Recipe for chocolate cake with frosting",
            ],
            metadatas=[
                {"type": "vault", "vault_type": "note", "directory": "notes",
                 "title": "Goals", "importance": "high"},
                {"type": "vault", "vault_type": "note", "directory": "notes",
                 "title": "Recipes", "importance": "low"},
            ],
        )

        # High threshold should return fewer/no results for off-topic query
        result = semantic_context("chocolate cake recipe", threshold=0.9)
        # With very high threshold, only extremely relevant results pass
        for match in result["matches"]:
            assert match["relevance"] >= 0.9

    def test_max_results_cap(self, mock_config):
        """Never returns more than max_results."""
        from tools.memory import _get_collection
        from tools.query import semantic_context

        collection = _get_collection()
        # Add many documents
        ids = [f"vault::notes/doc{i}.md" for i in range(10)]
        docs = [f"Document about career goals topic {i}" for i in range(10)]
        metas = [
            {"type": "vault", "vault_type": "note", "directory": "notes",
             "title": f"Doc {i}", "importance": "medium"}
            for i in range(10)
        ]
        collection.add(ids=ids, documents=docs, metadatas=metas)

        result = semantic_context("career goals", max_results=3, threshold=0.0)
        assert len(result["matches"]) <= 3

    def test_sensitive_dirs_excluded(self, mock_config):
        """Results from documents/ and people/ are never returned."""
        from tools.memory import _get_collection
        from tools.query import semantic_context

        collection = _get_collection()
        collection.add(
            ids=["vault::notes/safe.md", "vault::documents/sensitive.md", "vault::people/contact.md"],
            documents=[
                "Career goals and plans",
                "Career goals from sensitive document",
                "Career goals from people contact",
            ],
            metadatas=[
                {"type": "vault", "vault_type": "note", "directory": "notes",
                 "title": "Safe", "importance": "medium"},
                {"type": "vault", "vault_type": "note", "directory": "documents",
                 "title": "Sensitive", "importance": "high"},
                {"type": "vault", "vault_type": "note", "directory": "people",
                 "title": "Contact", "importance": "high"},
            ],
        )

        result = semantic_context("career goals", threshold=0.0)
        sources = [m["source"] for m in result["matches"]]
        assert "notes/safe.md" in sources
        assert "documents/sensitive.md" not in sources
        assert "people/contact.md" not in sources
        assert result["skipped_sensitive"] >= 2

    def test_content_preview_length(self, mock_config):
        """Content is truncated to max_content_length."""
        from tools.memory import _get_collection
        from tools.query import semantic_context

        collection = _get_collection()
        long_content = "Important career goal information. " * 100  # Very long
        collection.add(
            ids=["vault::notes/long.md"],
            documents=[long_content],
            metadatas=[
                {"type": "vault", "vault_type": "note", "directory": "notes",
                 "title": "Long", "importance": "high"},
            ],
        )

        result = semantic_context("career goals", max_content_length=100, threshold=0.0)
        if result["matches"]:
            assert len(result["matches"][0]["content"]) <= 110  # Allow for "..."

    def test_chunk_dedup(self, mock_config):
        """Only best chunk per parent file is returned."""
        from tools.memory import _get_collection
        from tools.query import semantic_context

        collection = _get_collection()
        collection.add(
            ids=["vault::notes/goals.md#chunk-0", "vault::notes/goals.md#chunk-1"],
            documents=[
                "Career goals for 2026 include leadership",
                "Other section about hobbies and travel",
            ],
            metadatas=[
                {"type": "vault", "vault_type": "note", "directory": "notes",
                 "title": "Goals", "importance": "high",
                 "parent_file": "notes/goals.md", "chunk_heading": "Career"},
                {"type": "vault", "vault_type": "note", "directory": "notes",
                 "title": "Goals", "importance": "medium",
                 "parent_file": "notes/goals.md", "chunk_heading": "Hobbies"},
            ],
        )

        result = semantic_context("career goals", threshold=0.0)
        # Should only return 1 result (best chunk for goals.md)
        sources = [m["source"] for m in result["matches"]]
        assert sources.count("notes/goals.md") <= 1


# --- Per-Prompt Config Tests ---

class TestPerPromptConfig:
    """Tests for get_per_prompt_config()."""

    def test_defaults_when_no_config(self, mock_config):
        """Missing memory config uses defaults."""
        from tools.config import get_per_prompt_config
        config = get_per_prompt_config()
        assert config["enabled"] is True
        assert config["threshold"] == 0.5
        assert config["max_results"] == 5
        assert config["max_content_length"] == 500
        assert config["passive_retrieval_increment"] == 0.01

    def test_disabled(self, mock_config):
        """Config can disable per-prompt search."""
        import tools.config as config_module
        config_module._config_cache = None

        config_data = json.loads(mock_config.path.read_text())
        config_data.setdefault("memory", {})["per_prompt_search"] = {"enabled": False}
        mock_config.path.write_text(json.dumps(config_data))
        config_module._config_cache = None

        from tools.config import get_per_prompt_config
        config = get_per_prompt_config()
        assert config["enabled"] is False

    def test_custom_threshold(self, mock_config):
        """Custom threshold overrides default."""
        import tools.config as config_module
        config_module._config_cache = None

        config_data = json.loads(mock_config.path.read_text())
        config_data.setdefault("memory", {})["per_prompt_search"] = {"threshold": 0.7}
        mock_config.path.write_text(json.dumps(config_data))
        config_module._config_cache = None

        from tools.config import get_per_prompt_config
        config = get_per_prompt_config()
        assert config["threshold"] == 0.7
        # Other defaults preserved
        assert config["enabled"] is True
        assert config["max_results"] == 5

    def test_custom_max_results(self, mock_config):
        """Custom max_results overrides default."""
        import tools.config as config_module
        config_module._config_cache = None

        config_data = json.loads(mock_config.path.read_text())
        config_data.setdefault("memory", {})["per_prompt_search"] = {"max_results": 3}
        mock_config.path.write_text(json.dumps(config_data))
        config_module._config_cache = None

        from tools.config import get_per_prompt_config
        config = get_per_prompt_config()
        assert config["max_results"] == 3
