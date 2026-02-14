---
name: test
description: This skill should be used when the user asks to "run tests", "run all tests", "test mcp server", "run unit tests", "run pytest", "test agents", "run LLM tests", or mentions testing any part of the Jarvis plugin. Unified test runner for both MCP server unit tests (pytest) and LLM agent tests.
---

# Jarvis Unified Test Runner

Unified entry point for running all Jarvis plugin tests. Covers two distinct test systems and provides a single interface to run either or both.

## Test Systems

### 1. MCP Server Unit Tests (pytest)

**What:** Deterministic Python unit tests for the jarvis-core MCP server.
**Location:** `plugins/jarvis/mcp-server/tests/`
**Runner:** pytest
**Speed:** Fast (~5-15 seconds)
**Cost:** Free (no API calls)

Test files:
- `test_config.py` - Config loading, verification, diagnostics
- `test_file_ops.py` - Vault file operations, security boundaries
- `test_commit.py` - JARVIS protocol commit formatting
- `test_git_common.py` - Git utility functions
- `test_git_ops.py` - Git history queries
- `test_memory.py` - ChromaDB memory operations
- `test_protocol.py` - Protocol validation
- `test_server.py` - Server registration, tool wiring

### 2. LLM Agent Tests (jarvis-test-runner)

**What:** Non-deterministic behavioral tests for Jarvis agents using multi-trial execution.
**Location:** `tests/agents/*.tests.json`
**Runner:** jarvis-test-runner skill (delegates via Task tool)
**Speed:** Slow (~30 seconds per test per trial)
**Cost:** API calls (~$0.15-0.25 per trial)

Current test suites:
- `jarvis-explorer-agent.tests.json` - 32 test cases

## Execution Workflow

### Parse User Intent

Determine which tests to run from user input:

| User says | Action |
|-----------|--------|
| "run tests" / "run all tests" | Run both: pytest first, then LLM tests |
| "run unit tests" / "run pytest" / "test mcp" | Run pytest only |
| "test agents" / "run LLM tests" / "run agent tests" | Run LLM agent tests only |
| "run tests for [specific file]" | Run matching pytest file or agent test |

### Step 1: Run MCP Server Unit Tests

Always run these first (fast, free, catches regressions early).

Execute via Bash:

```bash
cd plugins/jarvis/mcp-server && uv run pytest -v --tb=short
```

**Interpreting results:**
- All green: MCP server code is healthy
- Failures: Fix before proceeding to LLM tests (broken MCP = broken agents)

**With coverage (if user asks):**

```bash
cd plugins/jarvis/mcp-server && uv run pytest --cov=. --cov-report=term-missing
```

**Single test file:**

```bash
cd plugins/jarvis/mcp-server && uv run pytest tests/test_file_ops.py -v
```

### Step 2: Run LLM Agent Tests (if requested)

Only run if user explicitly asked for agent tests or "all tests". These cost money and take time.

**Before running, confirm with user:**
- Number of tests and estimated cost/time
- Trial count (default: 3)

**Delegate to jarvis-test-runner skill:**

```
Skill: jarvis-test-runner
Args: [user's scope and options]
```

The jarvis-test-runner handles discovery, execution, assertion validation, and reporting. See its SKILL.md for full details.

### Step 3: Report Summary

After all tests complete, present a unified summary:

```
## Test Results

### MCP Server (pytest)
- X tests passed, Y failed, Z skipped
- Duration: N seconds

### LLM Agent Tests (if run)
- X/Y tests passed (pass@k)
- Duration: N minutes
- Report: [scratchpad path]
```

## Quick Reference

| Command | What it runs | Time | Cost |
|---------|-------------|------|------|
| `/test` | Both systems | ~2-30 min | $0-24 |
| `/test unit` | pytest only | ~10 sec | Free |
| `/test agents` | LLM tests only | ~28 min | ~$14-24 |
| `/test agents EXP-001` | Single agent test | ~1 min | ~$0.50 |

## Error Handling

**pytest not found / import errors:**
```bash
cd plugins/jarvis/mcp-server && uv pip install -e ".[dev]"
```
Then retry.

**No test files found:**
Verify paths exist: `plugins/jarvis/mcp-server/tests/` and `tests/agents/`

**Agent test failures:**
Check if MCP server tests pass first. Broken server code causes agent test failures.

## Notes

- Always run pytest before LLM tests (fast feedback, catches root causes)
- MCP server tests are the primary regression safety net
- LLM tests validate agent behavior but only structural correctness (not semantic quality)
- Use `--tb=long` for detailed pytest tracebacks when debugging
