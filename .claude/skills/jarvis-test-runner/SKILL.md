---
name: jarvis-test-runner
description: This skill should be used when the user asks to "run tests", "test the plugin", "execute test suite", "validate agents", "run agent tests", or mentions testing Jarvis agents or skills. Automated test execution for Jarvis plugin with multi-trial support and pass@k metrics.
version: 0.1.0
---

# Jarvis Test Runner

Automated test execution system for Jarvis plugin agents and skills. Executes test suites with configurable trials, validates responses against assertions, calculates pass@k metrics, and generates detailed reports.

## When to Use This Skill

This skill should be used when the user asks to:
- Run tests for Jarvis agents or skills
- Execute test suites
- Validate agent behavior
- Check if changes broke existing functionality
- Generate test reports

## Core Concepts

### Test Files

Test definitions live in JSON files:
- `tests/agents/*.tests.json` - Agent test suites
- `tests/skills/*.tests.json` - Skill test suites

Each file contains test cases with input (JSON to send to agent) and expect (assertions to validate response).

### Multi-Trial Execution

LLM outputs are non-deterministic. Run each test multiple times (trials) to measure reliability:
- **pass@k**: Test passes if ANY trial succeeds (relaxed)
- **pass^k**: Test passes if ALL trials succeed (strict)
- **majority**: Test passes if >50% of trials succeed

### Assertion System

Validate agent responses using structured assertions:
- Status checks (`status`, `error_code`)
- Field validation (`has_fields`, `field_equals`, `field_contains`)
- Array checks (`all_match`, `all_match_pattern`, `none_match_pattern`)
- Conditional logic (`one_of`, `if_results`)

See `references/assertions.md` for complete assertion reference.

## Usage

### Basic Usage

```
Run all tests
Run tests for jarvis-explorer-agent
Run core suite tests
Run test EXP-001 with 5 trials
```

### Parameters

Parse from user input:

| Parameter | Default | Values | Description |
|-----------|---------|--------|-------------|
| `scope` | `"all"` | `all`, `agent:name`, `suite:name`, `test:ID` | What to test |
| `trials` | `3` | 1-10 | Runs per test |
| `pass_threshold` | `"any"` | `any`, `all`, `majority` | Pass criteria |
| `fail_fast` | `false` | `true`, `false` | Stop on first failure |

### Scope Examples

- `all` - Run all test files
- `agent:jarvis-explorer-agent` - Run tests/agents/jarvis-explorer-agent.tests.json
- `suite:core` - Run only "core" suite across all files
- `test:EXP-001` - Run single test by ID

## Test Execution Workflow

### Step 1: Discover Test Files

Use Glob to find test files matching scope:

```
test_files = Glob("tests/agents/*.tests.json")
test_files += Glob("tests/skills/*.tests.json")

# Filter by scope
if scope == "agent:name":
  test_files = filter files matching name
```

### Step 2: Load and Parse

For each test file:

```
content = Read(test_file)
parsed = JSON.parse(content)

# Extract metadata
agent = parsed.agent  // e.g., "jarvis:jarvis-explorer-agent"
suites = parsed.test_suites

# Filter by scope if needed
if scope == "suite:name":
  filter to that suite
if scope == "test:ID":
  find test with that ID
```

### Step 3: Execute Tests

For each test in scope:

```
# Skip if conditions met
if test.skip_if:
  evaluate condition → skip test if true

# Run trials
trial_results = []
for trial in 1..trials:
  start_time = now()

  # Delegate to target agent
  response = Task(
    subagent_type: agent,  // e.g., "jarvis:jarvis-explorer-agent"
    prompt: JSON.stringify(test.input),
    description: `Test ${test.id} - Trial ${trial}/${trials}`,
    model: "haiku"  // Use haiku for speed unless test specifies otherwise
  )

  end_time = now()

  # Validate response
  passed = validateAllAssertions(response, test.expect)

  trial_results.push({
    trial: trial,
    status: passed ? "pass" : "fail",
    duration_ms: end_time - start_time,
    failures: passed ? [] : getFailedAssertions(response, test.expect)
  })

  # Fail fast check
  if fail_fast and not passed:
    break

# Calculate metrics
pass_at_k = trial_results.some(t => t.status == "pass")
pass_all_k = trial_results.every(t => t.status == "pass")
pass_majority = trial_results.filter(t => t.status == "pass").length > trials / 2

# Determine final status
final_status =
  pass_threshold == "any" ? pass_at_k :
  pass_threshold == "all" ? pass_all_k :
  pass_majority
```

### Step 4: Generate Report

Write markdown report to scratchpad:

```
report_path = `${scratchpad}/test-report-${timestamp}.md`
Write(report_path, generateMarkdownReport(results))
```

## Assertion Validation

Validate responses using the assertion system. See `references/assertions.md` for complete details.

### Quick Reference

**Status Checks:**
- `status: "success"` - Response status equals value
- `error_code: "NO_FILTERS"` - Error code equals value

**Field Checks:**
- `has_fields: ["results", "summary"]` - Fields exist (dot notation supported)
- `field_equals: {"pagination.offset": 0}` - Field equals value
- `field_contains: {path: "summary", text: "found"}` - Field contains text

**Array Checks:**
- `all_match: {path: "results[*].type", value: "note"}` - All items equal value
- `all_match_pattern: {path: "results[*].file", regex: "^journal/"}` - All match regex
- `none_match_pattern: {path: "results[*].file", regex: "^people/"}` - None match regex

**Conditional:**
- `one_of: [{status: "error"}, {contains: "filter"}]` - At least one passes
- `if_results: {all_have_tags: ["work"]}` - Check only if results exist

Implement validation using the pseudocode in `references/assertions.md`.

## Report Format

Generate structured markdown report with:

### Executive Summary
- Total tests, passed, failed, skipped
- Pass rate percentage
- Total duration and average per test

### Test Results
- ✅ PASSED section with brief details
- ❌ FAILED section with assertion failures and diffs
- ⊘ SKIPPED section with skip reasons

### Performance Analysis
- Table of suite performance
- Duration statistics
- Slowest tests

### Recommendations
- Action items for failed tests
- Suggestions for improvements

See `examples/sample-report.md` for template.

## Error Handling

**No test files found:**
```
Return error: "NO_TEST_FILES_FOUND"
Message: "No test files found matching scope: {scope}"
Suggestion: "Check scope parameter or verify test files exist"
```

**Invalid test file:**
```
Skip file and log error
Continue with remaining files
Include in report as skipped with reason
```

**Agent not found:**
```
Skip test with reason: "Agent {name} not available"
Include in skipped section of report
```

**Assertion failure:**
```
Log detailed diff:
- Assertion type
- Expected value
- Actual value
- Path (for nested assertions)
Include in test failure details
```

## Implementation Notes

### Performance

- Use `model: "haiku"` for test execution (faster, cheaper)
- Run trials sequentially (not parallel) to avoid rate limits
- Cache test file parsing to avoid re-reading

### Context Management

- Don't load full test files into context unnecessarily
- Parse JSON, extract relevant tests, discard rest
- Keep only active test case data in memory

### Reporting

- Write to scratchpad (temporary), not committed files
- Include timestamp in report filename
- Generate both structured JSON and markdown

## Additional Resources

### Reference Files

For detailed implementation guidance:
- **`references/assertions.md`** - Complete assertion reference with implementation pseudocode
- **`references/test-schema.md`** - Test file JSON schema documentation

### Examples

Working examples in `examples/`:
- **`sample-report.md`** - Example test report format
- **`sample-test.json`** - Example test file structure

## Quick Start

When invoked:

1. Parse user input for scope and options
2. Discover test files using Glob
3. Load and parse matching test files
4. Execute tests with trials using Task delegation
5. Validate responses against assertions
6. Calculate pass@k metrics
7. Generate markdown report to scratchpad
8. Display summary and report path

Begin test execution now.
