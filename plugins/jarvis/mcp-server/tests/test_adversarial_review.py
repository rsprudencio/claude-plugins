"""Tests for adversarial_review module (standalone CLI at hooks-handlers/)."""

import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock

# Import from standalone hooks-handlers location
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers"))

from adversarial_review import (
    _normalize_timeout,
    _normalize_max_findings,
    _resolve_working_directory,
    _build_prompt,
    _extract_json_object,
    _normalize_review_payload,
    _codex_build_command,
    _codex_read_response,
    adversarial_review,
    DEFAULT_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    DEFAULT_MAX_FINDINGS,
    MAX_ALLOWED_FINDINGS,
    PROVIDERS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SAMPLE_REVIEW = {
    "status": "needs_revision",
    "summary": "Plan has critical gaps in error handling.",
    "findings": [
        {
            "id": "F1",
            "severity": "high",
            "title": "Missing retry logic",
            "problem": "No retry on transient failures",
            "impact": "Silent data loss",
            "fix": "Add exponential backoff",
        }
    ],
    "counter_proposal": {
        "has_alternative": False,
        "description": "",
        "trade_offs": "",
    },
    "agreement": {
        "strengths": ["Clean separation of concerns"],
        "well_handled": ["Error boundaries"],
    },
}


def _make_mock_run(response: dict, returncode: int = 0):
    """Create a mock subprocess.run that writes response to output file."""

    def mock_run(cmd, **kwargs):
        for i, arg in enumerate(cmd):
            if arg == "--output-last-message" and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_text(json.dumps(response))
                break
        return Mock(returncode=returncode, stdout="", stderr="")

    return mock_run


# ---------------------------------------------------------------------------
# TestNormalizeTimeout
# ---------------------------------------------------------------------------


class TestNormalizeTimeout:
    def test_default_on_none(self):
        assert _normalize_timeout(None) == DEFAULT_TIMEOUT_SECONDS

    def test_default_on_string(self):
        assert _normalize_timeout("not a number") == DEFAULT_TIMEOUT_SECONDS

    def test_clamps_below_minimum(self):
        assert _normalize_timeout(1) == MIN_TIMEOUT_SECONDS

    def test_clamps_above_maximum(self):
        assert _normalize_timeout(9999) == MAX_TIMEOUT_SECONDS

    def test_accepts_valid_value(self):
        assert _normalize_timeout(120) == 120

    def test_boundary_minimum(self):
        assert _normalize_timeout(MIN_TIMEOUT_SECONDS) == MIN_TIMEOUT_SECONDS

    def test_boundary_maximum(self):
        assert _normalize_timeout(MAX_TIMEOUT_SECONDS) == MAX_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# TestNormalizeMaxFindings
# ---------------------------------------------------------------------------


class TestNormalizeMaxFindings:
    def test_default_on_none(self):
        assert _normalize_max_findings(None) == DEFAULT_MAX_FINDINGS

    def test_default_on_string(self):
        assert _normalize_max_findings("abc") == DEFAULT_MAX_FINDINGS

    def test_clamps_below_one(self):
        assert _normalize_max_findings(0) == 1

    def test_clamps_above_max(self):
        assert _normalize_max_findings(100) == MAX_ALLOWED_FINDINGS

    def test_accepts_valid_value(self):
        assert _normalize_max_findings(5) == 5


# ---------------------------------------------------------------------------
# TestResolveWorkingDirectory
# ---------------------------------------------------------------------------


class TestResolveWorkingDirectory:
    @patch("adversarial_review._get_vault_path")
    def test_none_returns_vault(self, mock_vault, tmp_path):
        mock_vault.return_value = str(tmp_path)
        assert _resolve_working_directory(None) == str(tmp_path)

    @patch("adversarial_review._get_vault_path")
    def test_empty_string_returns_vault(self, mock_vault, tmp_path):
        mock_vault.return_value = str(tmp_path)
        assert _resolve_working_directory("") == str(tmp_path)

    @patch("adversarial_review._get_vault_path")
    def test_nonexistent_dir_returns_vault(self, mock_vault, tmp_path):
        mock_vault.return_value = str(tmp_path)
        assert _resolve_working_directory("/nonexistent/path") == str(tmp_path)

    @patch("adversarial_review._get_vault_path")
    def test_outside_vault_returns_vault(self, mock_vault, tmp_path):
        mock_vault.return_value = str(tmp_path)
        assert _resolve_working_directory("/tmp") == str(tmp_path)

    @patch("adversarial_review._get_vault_path")
    def test_valid_subdir_accepted(self, mock_vault, tmp_path):
        subdir = tmp_path / "notes"
        subdir.mkdir()
        mock_vault.return_value = str(tmp_path)
        assert _resolve_working_directory(str(subdir)) == str(subdir)

    @patch("adversarial_review._get_vault_path")
    def test_vault_root_accepted(self, mock_vault, tmp_path):
        mock_vault.return_value = str(tmp_path)
        assert _resolve_working_directory(str(tmp_path)) == str(tmp_path)

    @patch("adversarial_review._get_vault_path")
    def test_symlink_resolved(self, mock_vault, tmp_path):
        """Symlinks pointing outside vault are rejected."""
        mock_vault.return_value = str(tmp_path)
        link = tmp_path / "link"
        link.symlink_to("/tmp")
        assert _resolve_working_directory(str(link)) == str(tmp_path)


# ---------------------------------------------------------------------------
# TestBuildPrompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_minimal_prompt(self):
        result = _build_prompt("Deploy service X")
        assert "Deploy service X" in result
        assert "Plan Under Review" in result
        assert "Response Requirements" in result

    def test_with_context(self):
        result = _build_prompt("Plan A", context="We use AWS")
        assert "Additional Context" in result
        assert "We use AWS" in result

    def test_with_assumptions(self):
        result = _build_prompt("Plan A", assumptions=["Python 3.12", "Linux"])
        assert "Stated Assumptions" in result
        assert "- Python 3.12" in result
        assert "- Linux" in result

    def test_with_focus_areas(self):
        result = _build_prompt("Plan A", focus_areas=["Security", "Performance"])
        assert "Focus Areas" in result
        assert "- Security" in result

    def test_max_findings_in_prompt(self):
        result = _build_prompt("Plan A", max_findings=3)
        assert "max 3 findings" in result


# ---------------------------------------------------------------------------
# TestExtractJsonObject
# ---------------------------------------------------------------------------


class TestExtractJsonObject:
    def test_direct_json(self):
        obj = {"status": "approved"}
        result = _extract_json_object(json.dumps(obj))
        assert result == obj

    def test_fenced_code_block(self):
        text = 'Some preamble\n```json\n{"status": "approved"}\n```\nSome postamble'
        result = _extract_json_object(text)
        assert result == {"status": "approved"}

    def test_fenced_without_json_tag(self):
        text = 'Preamble\n```\n{"status": "approved"}\n```'
        result = _extract_json_object(text)
        assert result == {"status": "approved"}

    def test_balanced_brace(self):
        text = 'Here is my review: {"status": "approved", "summary": "LGTM"} end.'
        result = _extract_json_object(text)
        assert result["status"] == "approved"

    def test_empty_string(self):
        assert _extract_json_object("") is None

    def test_none_input(self):
        assert _extract_json_object(None) is None

    def test_no_json(self):
        assert _extract_json_object("This has no JSON at all") is None

    def test_array_not_accepted(self):
        assert _extract_json_object('[1, 2, 3]') is None

    def test_nested_json(self):
        obj = {"outer": {"inner": "value"}}
        result = _extract_json_object(json.dumps(obj))
        assert result == obj

    def test_whitespace_padded(self):
        result = _extract_json_object('  \n  {"key": "val"}  \n  ')
        assert result == {"key": "val"}

    def test_escaped_quotes(self):
        text = '{"msg": "He said \\"hello\\""}'
        result = _extract_json_object(text)
        assert result is not None
        assert "hello" in result["msg"]


# ---------------------------------------------------------------------------
# TestNormalizeReviewPayload
# ---------------------------------------------------------------------------


class TestNormalizeReviewPayload:
    def test_valid_payload_passes_through(self):
        result = _normalize_review_payload(SAMPLE_REVIEW, 8)
        assert result["status"] == "needs_revision"
        assert len(result["findings"]) == 1
        assert result["findings"][0]["severity"] == "high"

    def test_invalid_status_defaults(self):
        raw = {**SAMPLE_REVIEW, "status": "banana"}
        result = _normalize_review_payload(raw, 8)
        assert result["status"] == "needs_revision"

    def test_missing_status_defaults(self):
        raw = {k: v for k, v in SAMPLE_REVIEW.items() if k != "status"}
        result = _normalize_review_payload(raw, 8)
        assert result["status"] == "needs_revision"

    def test_invalid_severity_defaults_to_medium(self):
        raw = {
            **SAMPLE_REVIEW,
            "findings": [{"severity": "extreme", "title": "Bad"}],
        }
        result = _normalize_review_payload(raw, 8)
        assert result["findings"][0]["severity"] == "medium"

    def test_findings_clamped_to_max(self):
        findings = [{"id": f"F{i}", "title": f"Issue {i}"} for i in range(20)]
        raw = {**SAMPLE_REVIEW, "findings": findings}
        result = _normalize_review_payload(raw, 3)
        assert len(result["findings"]) == 3

    def test_missing_findings_defaults_to_empty(self):
        raw = {k: v for k, v in SAMPLE_REVIEW.items() if k != "findings"}
        result = _normalize_review_payload(raw, 8)
        assert result["findings"] == []

    def test_findings_not_list_defaults_to_empty(self):
        raw = {**SAMPLE_REVIEW, "findings": "not a list"}
        result = _normalize_review_payload(raw, 8)
        assert result["findings"] == []

    def test_non_dict_finding_skipped(self):
        raw = {**SAMPLE_REVIEW, "findings": ["not a dict", {"title": "Real"}]}
        result = _normalize_review_payload(raw, 8)
        assert len(result["findings"]) == 1

    def test_missing_counter_proposal(self):
        raw = {k: v for k, v in SAMPLE_REVIEW.items() if k != "counter_proposal"}
        result = _normalize_review_payload(raw, 8)
        assert result["counter_proposal"]["has_alternative"] is False

    def test_missing_agreement(self):
        raw = {k: v for k, v in SAMPLE_REVIEW.items() if k != "agreement"}
        result = _normalize_review_payload(raw, 8)
        assert result["agreement"]["strengths"] == []

    def test_type_coercion(self):
        """Numeric/boolean values in string fields get coerced."""
        raw = {
            "status": "approved",
            "summary": 42,
            "findings": [],
            "counter_proposal": {"has_alternative": 1, "description": 0},
            "agreement": {"strengths": [], "well_handled": []},
        }
        result = _normalize_review_payload(raw, 8)
        assert result["summary"] == "42"
        assert result["counter_proposal"]["has_alternative"] is True
        assert result["counter_proposal"]["description"] == "0"


# ---------------------------------------------------------------------------
# TestCodexBuildCommand
# ---------------------------------------------------------------------------


class TestCodexBuildCommand:
    def test_basic_command_structure(self):
        cmd = _codex_build_command(
            binary="/usr/bin/codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert cmd[0] == "/usr/bin/codex"
        assert cmd[1] == "exec"
        assert "--sandbox" in cmd
        idx = cmd.index("--sandbox")
        assert cmd[idx + 1] == "read-only"

    def test_stdin_marker_present(self):
        cmd = _codex_build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert cmd[-1] == "-"

    def test_model_override(self):
        cmd = _codex_build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
            model="o3",
        )
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "o3"

    def test_profile_override(self):
        cmd = _codex_build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
            profile="review",
        )
        assert "--profile" in cmd
        idx = cmd.index("--profile")
        assert cmd[idx + 1] == "review"

    def test_ephemeral_flag_present(self):
        cmd = _codex_build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "--ephemeral" in cmd

    def test_output_schema_present(self):
        cmd = _codex_build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "--output-schema" in cmd
        idx = cmd.index("--output-schema")
        assert cmd[idx + 1] == "/tmp/schema.json"


# ---------------------------------------------------------------------------
# TestCodexReadResponse
# ---------------------------------------------------------------------------


class TestCodexReadResponse:
    def test_reads_from_file(self, tmp_path):
        out_file = tmp_path / "response.md"
        out_file.write_text('{"status": "approved"}')
        result = _codex_read_response(str(out_file), "fallback")
        assert result == '{"status": "approved"}'

    def test_falls_back_to_stdout(self, tmp_path):
        result = _codex_read_response("/nonexistent/path", "stdout output")
        assert result == "stdout output"

    def test_empty_file_falls_back(self, tmp_path):
        out_file = tmp_path / "response.md"
        out_file.write_text("   ")
        result = _codex_read_response(str(out_file), "fallback content")
        assert result == "fallback content"


# ---------------------------------------------------------------------------
# TestAdversarialReview
# ---------------------------------------------------------------------------


class TestAdversarialReview:
    @patch("adversarial_review._which", return_value=None)
    def test_binary_missing(self, mock_which):
        result = adversarial_review(plan="Test plan")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_empty_plan(self):
        result = adversarial_review(plan="")
        assert result["success"] is False
        assert "required" in result["error"].lower()

    def test_whitespace_plan(self):
        result = adversarial_review(plan="   ")
        assert result["success"] is False
        assert "required" in result["error"].lower()

    def test_unknown_provider(self):
        result = adversarial_review(plan="Test", provider="opencode")
        assert result["success"] is False
        assert "Unknown provider" in result["error"]

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_timeout(self, mock_run, mock_which, mock_vault):
        import subprocess as sp

        mock_run.side_effect = sp.TimeoutExpired(cmd="codex", timeout=240)
        result = adversarial_review(plan="Test plan")
        assert result["success"] is False
        assert "timed out" in result["error"]

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_nonzero_exit_no_output(self, mock_run, mock_which, mock_vault):
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="some error")
        result = adversarial_review(plan="Test plan")
        assert result["success"] is False
        assert "exited with code 1" in result["error"]

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_unparseable_output(self, mock_run, mock_which, mock_vault):
        mock_run.return_value = Mock(
            returncode=0, stdout="Not JSON at all", stderr=""
        )
        result = adversarial_review(plan="Test plan")
        assert result["success"] is False
        assert "parse" in result["error"].lower()

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_include_raw_on_failure(self, mock_run, mock_which, mock_vault):
        mock_run.return_value = Mock(
            returncode=0, stdout="garbage output", stderr=""
        )
        result = adversarial_review(plan="Test plan", include_raw=True)
        assert result["success"] is False
        assert "raw_output" in result

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_successful_review(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(SAMPLE_REVIEW)
        result = adversarial_review(plan="Test plan")
        assert result["success"] is True
        assert result["provider"] == "codex"
        assert result["review"]["status"] == "needs_revision"
        assert len(result["review"]["findings"]) == 1

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_include_raw_on_success(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(SAMPLE_REVIEW)
        result = adversarial_review(plan="Test plan", include_raw=True)
        assert result["success"] is True
        assert "raw_output" in result

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_prompt_passed_via_stdin(self, mock_run, mock_which, mock_vault):
        """Verify prompt is passed as stdin input, not as argv."""
        mock_run.side_effect = _make_mock_run(SAMPLE_REVIEW)
        adversarial_review(plan="My detailed plan here")

        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("input") is not None
        assert "My detailed plan here" in call_kwargs.kwargs["input"]

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_nonzero_exit_with_valid_output(self, mock_run, mock_which, mock_vault):
        """Nonzero exit code but valid output file should still succeed."""
        mock_run.side_effect = _make_mock_run(SAMPLE_REVIEW, returncode=1)
        result = adversarial_review(plan="Test plan")
        assert result["success"] is True


# ---------------------------------------------------------------------------
# TestCommandSafety
# ---------------------------------------------------------------------------


class TestCommandSafety:
    """Invariant tests for command construction safety."""

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_sandbox_read_only_always_present(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(SAMPLE_REVIEW)
        adversarial_review(plan="Test plan")

        cmd = mock_run.call_args[0][0]
        assert "--sandbox" in cmd
        idx = cmd.index("--sandbox")
        assert cmd[idx + 1] == "read-only"

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_no_approve_flag(self, mock_run, mock_which, mock_vault):
        """The -a / --approve flag must never appear (invalid for codex exec)."""
        mock_run.side_effect = _make_mock_run(SAMPLE_REVIEW)
        adversarial_review(plan="Test plan")

        cmd = mock_run.call_args[0][0]
        assert "-a" not in cmd
        assert "--approve" not in cmd
        assert "--full-auto" not in cmd

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_stdin_prompt_marker(self, mock_run, mock_which, mock_vault):
        """The '-' stdin marker must be the last argument."""
        mock_run.side_effect = _make_mock_run(SAMPLE_REVIEW)
        adversarial_review(plan="Test plan")

        cmd = mock_run.call_args[0][0]
        assert cmd[-1] == "-"

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_output_schema_in_command(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(SAMPLE_REVIEW)
        adversarial_review(plan="Test plan")

        cmd = mock_run.call_args[0][0]
        assert "--output-schema" in cmd
