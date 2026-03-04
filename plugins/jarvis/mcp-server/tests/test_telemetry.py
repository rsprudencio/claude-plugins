"""Tests for telemetry module (LLM call cost tracking)."""

import json
import os
import sys
import tempfile
import pytest
from pathlib import Path

# Import from standalone hooks-handlers location
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers"))

from telemetry import estimate_cost, log_llm_call, _COST_TABLE


class TestEstimateCost:
    """Tests for estimate_cost()."""

    def test_haiku_cost(self):
        cost = estimate_cost("claude-haiku-4-5-20251001", 1000, 500)
        assert cost is not None
        # 1000 input @ $0.80/M + 500 output @ $4.00/M
        expected = (1000 / 1_000_000) * 0.80 + (500 / 1_000_000) * 4.00
        assert cost == round(expected, 6)

    def test_gpt4o_cost(self):
        cost = estimate_cost("gpt-4o", 10000, 1000)
        assert cost is not None
        expected = (10000 / 1_000_000) * 2.50 + (1000 / 1_000_000) * 10.00
        assert cost == round(expected, 6)

    def test_unknown_model(self):
        cost = estimate_cost("unknown-model-v1", 1000, 500)
        assert cost is None

    def test_zero_tokens(self):
        cost = estimate_cost("claude-haiku-4-5-20251001", 0, 0)
        assert cost == 0.0

    def test_large_token_count(self):
        cost = estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
        assert cost is not None
        expected = 0.80 + 4.00
        assert cost == round(expected, 6)

    def test_gemini_flash_cost(self):
        cost = estimate_cost("gemini-2.0-flash", 5000, 2000)
        assert cost is not None
        expected = (5000 / 1_000_000) * 0.10 + (2000 / 1_000_000) * 0.40
        assert cost == round(expected, 6)


class TestLogLlmCall:
    """Tests for log_llm_call()."""

    def test_writes_jsonl_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telemetry_file = Path(tmpdir) / "test_calls.jsonl"
            log_llm_call(
                provider="anthropic",
                model="claude-haiku-4-5-20251001",
                input_tokens=500,
                output_tokens=200,
                latency_ms=150,
                purpose="extraction",
                backend="API",
                telemetry_file=telemetry_file,
            )

            assert telemetry_file.exists()
            lines = telemetry_file.read_text().strip().split("\n")
            assert len(lines) == 1

            record = json.loads(lines[0])
            assert record["provider"] == "anthropic"
            assert record["model"] == "claude-haiku-4-5-20251001"
            assert record["input_tokens"] == 500
            assert record["output_tokens"] == 200
            assert record["latency_ms"] == 150
            assert record["purpose"] == "extraction"
            assert record["backend"] == "API"
            assert "cost_usd" in record
            assert record["ts"]  # timestamp present

    def test_appends_to_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telemetry_file = Path(tmpdir) / "test_calls.jsonl"
            for i in range(3):
                log_llm_call(
                    provider="anthropic",
                    model="claude-haiku-4-5-20251001",
                    input_tokens=100 * (i + 1),
                    output_tokens=50,
                    latency_ms=100,
                    purpose="test",
                    telemetry_file=telemetry_file,
                )

            lines = telemetry_file.read_text().strip().split("\n")
            assert len(lines) == 3

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telemetry_file = Path(tmpdir) / "nested" / "dir" / "calls.jsonl"
            log_llm_call(
                provider="codex",
                model="gpt-4o",
                input_tokens=100,
                output_tokens=50,
                latency_ms=200,
                purpose="review",
                telemetry_file=telemetry_file,
            )
            assert telemetry_file.exists()

    def test_custom_cost_estimate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telemetry_file = Path(tmpdir) / "test_calls.jsonl"
            log_llm_call(
                provider="anthropic",
                model="claude-haiku-4-5-20251001",
                input_tokens=100,
                output_tokens=50,
                latency_ms=100,
                purpose="test",
                cost_estimate=0.001234,
                telemetry_file=telemetry_file,
            )

            record = json.loads(telemetry_file.read_text().strip())
            assert record["cost_usd"] == 0.001234

    def test_unknown_model_no_cost(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telemetry_file = Path(tmpdir) / "test_calls.jsonl"
            log_llm_call(
                provider="custom",
                model="unknown-model",
                input_tokens=100,
                output_tokens=50,
                latency_ms=100,
                purpose="test",
                telemetry_file=telemetry_file,
            )

            record = json.loads(telemetry_file.read_text().strip())
            assert "cost_usd" not in record

    def test_never_raises_on_error(self):
        """Telemetry should never raise exceptions."""
        # Pass an invalid path that can't be created
        log_llm_call(
            provider="test",
            model="test",
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            purpose="test",
            telemetry_file=Path("/nonexistent/root/file.jsonl"),
        )
        # Should not raise


class TestCostTable:
    """Tests for the cost table completeness."""

    def test_anthropic_models_present(self):
        assert "claude-haiku-4-5-20251001" in _COST_TABLE
        assert "claude-haiku-4-5" in _COST_TABLE
        assert "claude-sonnet-4-6" in _COST_TABLE
        assert "claude-opus-4-6" in _COST_TABLE

    def test_openai_models_present(self):
        assert "gpt-4o" in _COST_TABLE
        assert "gpt-4o-mini" in _COST_TABLE

    def test_google_models_present(self):
        assert "gemini-2.0-flash" in _COST_TABLE

    def test_all_entries_have_input_output(self):
        for model, pricing in _COST_TABLE.items():
            assert "input" in pricing, f"{model} missing input price"
            assert "output" in pricing, f"{model} missing output price"
            assert pricing["input"] >= 0, f"{model} negative input price"
            assert pricing["output"] >= 0, f"{model} negative output price"
