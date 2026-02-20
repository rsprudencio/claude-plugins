"""Golden contract tests for adversarial_review module.

These tests lock the observable return shapes and behavioral invariants
of the adversarial review system BEFORE any refactoring. If any test
here breaks during the provider adapter extraction, the refactor
introduced a behavioral change it shouldn't have.

Contract invariants tested:
- Return shape: {success, provider, review} on success, {success, error} on failure
- Review fields: status, summary, findings[], counter_proposal, agreement
- Exit semantics: success=True even for needs_revision status
- Provider provenance always present when provider is known
- include_raw behavior on both success and failure paths
- Edge cases: empty plan, unknown provider, timeout, missing binary, nonzero exit + valid output
"""

import json
import os
import sys
import subprocess as sp
import pytest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers"))

from adversarial_review import (
    adversarial_review,
    _extract_json_object,
    _normalize_review_payload,
    PROVIDERS,
    _REVIEW_SCHEMA,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FULL_REVIEW = {
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
        },
        {
            "id": "F2",
            "severity": "critical",
            "title": "No auth check",
            "problem": "Endpoint is unauthenticated",
            "impact": "Unauthorized access",
            "fix": "Add auth middleware",
        },
    ],
    "counter_proposal": {
        "has_alternative": True,
        "description": "Use event-driven architecture instead",
        "trade_offs": "Higher complexity but better resilience",
    },
    "agreement": {
        "strengths": ["Clean separation of concerns", "Good test coverage"],
        "well_handled": ["Error boundaries", "Logging strategy"],
    },
}

APPROVED_REVIEW = {
    "status": "approved",
    "summary": "Plan is solid with minor suggestions.",
    "findings": [],
    "counter_proposal": {
        "has_alternative": False,
        "description": "",
        "trade_offs": "",
    },
    "agreement": {
        "strengths": ["Thorough design"],
        "well_handled": ["Edge case coverage"],
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
# TestSuccessReturnShape — locks the structure of successful responses
# ---------------------------------------------------------------------------


class TestSuccessReturnShape:
    """Contract: successful reviews always return {success, provider, review}."""

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_success_has_required_keys(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(FULL_REVIEW)
        result = adversarial_review(plan="Test plan")

        assert "success" in result
        assert "provider" in result
        assert "review" in result
        assert result["success"] is True

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_review_has_all_schema_fields(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(FULL_REVIEW)
        result = adversarial_review(plan="Test plan")

        review = result["review"]
        required_fields = {"status", "summary", "findings", "counter_proposal", "agreement"}
        assert set(review.keys()) == required_fields

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_findings_have_required_fields(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(FULL_REVIEW)
        result = adversarial_review(plan="Test plan")

        for finding in result["review"]["findings"]:
            for field in ("id", "severity", "title", "problem", "impact", "fix"):
                assert field in finding, f"Missing field '{field}' in finding"

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_counter_proposal_has_required_fields(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(FULL_REVIEW)
        result = adversarial_review(plan="Test plan")

        cp = result["review"]["counter_proposal"]
        assert "has_alternative" in cp
        assert "description" in cp
        assert "trade_offs" in cp

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_agreement_has_required_fields(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(FULL_REVIEW)
        result = adversarial_review(plan="Test plan")

        ag = result["review"]["agreement"]
        assert "strengths" in ag
        assert "well_handled" in ag
        assert isinstance(ag["strengths"], list)
        assert isinstance(ag["well_handled"], list)


# ---------------------------------------------------------------------------
# TestFailureReturnShape — locks the structure of failure responses
# ---------------------------------------------------------------------------


class TestFailureReturnShape:
    """Contract: failures always return {success: False, error: str}."""

    def test_empty_plan_failure_shape(self):
        result = adversarial_review(plan="")
        assert result["success"] is False
        assert "error" in result
        assert isinstance(result["error"], str)

    def test_unknown_provider_failure_shape(self):
        result = adversarial_review(plan="Test", provider="nonexistent")
        assert result["success"] is False
        assert "error" in result

    @patch("adversarial_review._which", return_value=None)
    def test_missing_binary_failure_shape(self, mock_which):
        result = adversarial_review(plan="Test plan")
        assert result["success"] is False
        assert "error" in result

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_timeout_failure_shape(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = sp.TimeoutExpired(cmd="codex", timeout=240)
        result = adversarial_review(plan="Test plan")

        assert result["success"] is False
        assert "error" in result
        assert "provider" in result  # provider known at timeout point

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_parse_failure_shape(self, mock_run, mock_which, mock_vault):
        mock_run.return_value = Mock(returncode=0, stdout="not json", stderr="")
        result = adversarial_review(plan="Test plan")

        assert result["success"] is False
        assert "error" in result
        assert "provider" in result


# ---------------------------------------------------------------------------
# TestExitSemantics — success=True even for needs_revision
# ---------------------------------------------------------------------------


class TestExitSemantics:
    """Contract: success=True reflects execution success, not plan approval."""

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_needs_revision_is_success(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(FULL_REVIEW)
        result = adversarial_review(plan="Test plan")

        assert result["success"] is True
        assert result["review"]["status"] == "needs_revision"

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_approved_is_success(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(APPROVED_REVIEW)
        result = adversarial_review(plan="Test plan")

        assert result["success"] is True
        assert result["review"]["status"] == "approved"

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_nonzero_exit_with_valid_output_is_success(self, mock_run, mock_which, mock_vault):
        """Nonzero exit code but parseable output → success=True."""
        mock_run.side_effect = _make_mock_run(FULL_REVIEW, returncode=1)
        result = adversarial_review(plan="Test plan")

        assert result["success"] is True

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_nonzero_exit_without_output_is_failure(self, mock_run, mock_which, mock_vault):
        """Nonzero exit code and no output → success=False."""
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="fatal error")
        result = adversarial_review(plan="Test plan")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestProviderProvenance — provider field present when known
# ---------------------------------------------------------------------------


class TestProviderProvenance:
    """Contract: provider field present whenever the provider is known."""

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_provider_on_success(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(FULL_REVIEW)
        result = adversarial_review(plan="Test plan", provider="codex")
        assert result["provider"] == "codex"

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_provider_on_timeout(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = sp.TimeoutExpired(cmd="codex", timeout=240)
        result = adversarial_review(plan="Test plan")
        assert result["provider"] == "codex"

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_provider_on_nonzero_exit(self, mock_run, mock_which, mock_vault):
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="error")
        result = adversarial_review(plan="Test plan")
        assert result["provider"] == "codex"

    def test_no_provider_on_unknown_provider(self):
        """Unknown provider errors don't include provider field."""
        result = adversarial_review(plan="Test", provider="nonexistent")
        assert "provider" not in result

    def test_no_provider_on_empty_plan(self):
        """Empty plan errors are caught before provider resolution."""
        result = adversarial_review(plan="")
        assert "provider" not in result


# ---------------------------------------------------------------------------
# TestIncludeRawBehavior — include_raw toggling
# ---------------------------------------------------------------------------


class TestIncludeRawBehavior:
    """Contract: raw_output included iff include_raw=True."""

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_raw_included_on_success(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(FULL_REVIEW)
        result = adversarial_review(plan="Test plan", include_raw=True)
        assert "raw_output" in result

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_raw_excluded_by_default_on_success(self, mock_run, mock_which, mock_vault):
        mock_run.side_effect = _make_mock_run(FULL_REVIEW)
        result = adversarial_review(plan="Test plan", include_raw=False)
        assert "raw_output" not in result

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_raw_included_on_parse_failure(self, mock_run, mock_which, mock_vault):
        mock_run.return_value = Mock(returncode=0, stdout="garbage", stderr="")
        result = adversarial_review(plan="Test plan", include_raw=True)
        assert result["success"] is False
        assert "raw_output" in result

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_raw_excluded_by_default_on_parse_failure(self, mock_run, mock_which, mock_vault):
        mock_run.return_value = Mock(returncode=0, stdout="garbage", stderr="")
        result = adversarial_review(plan="Test plan", include_raw=False)
        assert result["success"] is False
        assert "raw_output" not in result

    @patch("adversarial_review._get_vault_path", return_value="/tmp")
    @patch("adversarial_review._which", return_value="/usr/bin/codex")
    @patch("adversarial_review.subprocess.run")
    def test_raw_output_truncated_to_5000(self, mock_run, mock_which, mock_vault):
        """raw_output is capped at 5000 characters."""
        huge_review = {**FULL_REVIEW, "padding": "x" * 10000}
        mock_run.side_effect = _make_mock_run(huge_review)
        result = adversarial_review(plan="Test plan", include_raw=True)
        assert len(result["raw_output"]) <= 5000


# ---------------------------------------------------------------------------
# TestSchemaCompleteness — review schema matches normalization output
# ---------------------------------------------------------------------------


class TestSchemaCompleteness:
    """Contract: the JSON schema and normalization output are aligned."""

    def test_schema_required_fields_match_normalized_output(self):
        """Every required field in _REVIEW_SCHEMA appears in normalized output."""
        normalized = _normalize_review_payload({}, 8)
        for field in _REVIEW_SCHEMA["required"]:
            assert field in normalized, f"Schema requires '{field}' but normalization omits it"

    def test_normalized_output_keys_match_schema(self):
        """Normalized output doesn't contain fields outside the schema."""
        normalized = _normalize_review_payload(FULL_REVIEW, 8)
        schema_fields = set(_REVIEW_SCHEMA["properties"].keys())
        for key in normalized:
            assert key in schema_fields, f"Normalized output has '{key}' not in schema"

    def test_providers_registry_has_codex(self):
        """Codex provider is always registered."""
        assert "codex" in PROVIDERS
        assert "binary" in PROVIDERS["codex"]
        assert "build_command" in PROVIDERS["codex"]
        assert "read_response" in PROVIDERS["codex"]
