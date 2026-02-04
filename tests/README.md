# Jarvis Agent Test Suite

Automated testing framework for Jarvis agents and skills with declarative test definitions and assertion validation.

## Overview

This test framework allows you to:
- Define test cases in JSON files (one per agent/skill)
- Run automated tests against agents
- Validate responses with rich assertion language
- Track regressions and side effects
- Generate test reports

## Structure

```
tests/
├── test-schema.json              # JSON schema for test definitions
├── README.md                     # This file
├── agents/                       # Agent test files
│   ├── jarvis-explorer-agent.tests.json
│   ├── jarvis-journal-agent.tests.json
│   ├── jarvis-audit-agent.tests.json
│   └── jarvis-todoist-agent.tests.json
└── skills/                       # Skill test files
    ├── jarvis-orient.tests.json
    ├── jarvis-journal.tests.json
    └── jarvis-summarize.tests.json
```

## Test File Format

Each agent/skill has its own test file following this structure:

```json
{
  "agent": "jarvis:jarvis-explorer-agent",
  "version": "1.0.0",
  "description": "Test suite for jarvis-explorer-agent",

  "test_suites": {
    "suite_name": {
      "name": "Human Readable Suite Name",
      "priority": "critical",
      "tests": [
        {
          "id": "EXP-001",
          "name": "Test name",
          "description": "What this test validates",
          "input": { /* JSON input to agent */ },
          "expect": { /* Assertions */ }
        }
      ]
    }
  }
}
```

## Assertion Types

### Status & Basic Checks
```json
{
  "status": "success",           // Expected status
  "error_code": "NO_FILTERS",    // Expected error code
  "contains": "filter",          // Output contains text
  "message_contains": "error"    // Message field contains text
}
```

### Result Count
```json
{
  "results_min": 1,              // At least 1 result
  "results_max": 10,             // At most 10 results
  "results_count": 5             // Exactly 5 results
}
```

### Field Validation
```json
{
  "has_fields": ["results", "summary"],  // Required fields
  "field_equals": {                      // Field must equal value
    "pagination.offset": 0
  },
  "field_contains": {                    // Field contains text
    "path": "summary",
    "text": "no results"
  }
}
```

### Array Validation
```json
{
  "all_match": {                         // All items match value
    "path": "results[*].metadata.type",
    "value": "incident-log"
  },
  "all_match_one_of": {                  // All items match one of
    "path": "results[*].metadata.type",
    "values": ["note", "incident-log"]
  },
  "all_match_pattern": {                 // All match regex
    "path": "results[*].file",
    "regex": "^journal/"
  },
  "none_match_pattern": {                // None match regex
    "path": "results[*].file",
    "regex": "^(people/|documents/)"
  }
}
```

### Tag Validation
```json
{
  "all_have_tags": ["work", "security"],    // All have these tags
  "none_have_tags": ["draft"],              // None have these tags
  "each_has_any_tag": ["work", "personal"]  // Each has at least one
}
```

### Date Validation
```json
{
  "all_dates_between": {
    "path": "results[*].metadata.created",
    "start": "2026-01-01T00:00:00Z",
    "end": "2026-01-31T23:59:59Z"
  },
  "dates_within_days": {
    "path": "results[*].metadata.created",
    "days": 7
  }
}
```

### Range Checks
```json
{
  "all_in_range": {
    "path": "results[*].relevance",
    "min": 0.0,
    "max": 1.0
  },
  "range_check": {
    "path": "performance.search_duration_ms",
    "min": 0,
    "max": 5000
  }
}
```

### Conditional Assertions
```json
{
  "one_of": [                            // At least one must pass
    {"status": "error"},
    {"contains": "filter"}
  ],
  "if_results": {                        // If results exist, check:
    "all_have_tags": ["work"]
  },
  "if_has_history": {                    // If history_results exist
    "all_match": {
      "path": "history_results[*].operation",
      "value": "delete"
    }
  }
}
```

### Special Cases
```json
{
  "sorted_desc": {                       // Array sorted descending
    "path": "results[*].relevance"
  },
  "array_contains": {                    // Array contains values
    "path": "sensitive_dirs_skipped",
    "values": ["people/", "documents/"]
  }
}
```

## Multi-Step Tests

For tests requiring state between steps:

```json
{
  "id": "EXP-032",
  "name": "Pagination overlap check",
  "multi_step": [
    {
      "step": 1,
      "input": {"limit": 5, "offset": 0},
      "store": "page1_files"             // Store results
    },
    {
      "step": 2,
      "input": {"limit": 5, "offset": 5},
      "expect": {
        "no_overlap": {                  // Compare with stored
          "path": "results[*].file",
          "with": "page1_files"
        }
      }
    }
  ]
}
```

## Running Tests

### Run All Tests
```bash
# Via test runner agent
/jarvis-test-runner
```

### Run Specific Agent Tests
```bash
# Via test runner agent
/jarvis-test-runner --agent jarvis-explorer-agent
```

### Run Specific Suite
```bash
/jarvis-test-runner --agent jarvis-explorer-agent --suite core
```

### Run Single Test
```bash
/jarvis-test-runner --test EXP-001
```

## Test Priorities

| Priority | Description | When to Use |
|----------|-------------|-------------|
| **critical** | Core functionality | Must pass for production |
| **high** | Important features | Should pass for release |
| **medium** | Nice-to-have | Can be fixed later |
| **low** | Edge cases | Optional validation |

## Test Reports

Test runner generates reports in `tests/reports/`:

```
tests/reports/
├── 2026-02-03_23-45-00.md        # Full test report
└── latest.json                   # Latest results (JSON)
```

Report includes:
- Pass/Fail/Skip counts
- Failed test details with diffs
- Performance metrics
- Coverage statistics

## Writing New Tests

### 1. Create Test File

Create `tests/agents/your-agent.tests.json`:

```json
{
  "agent": "jarvis:your-agent",
  "version": "1.0.0",
  "description": "Test suite for your-agent",
  "test_suites": {
    "core": {
      "name": "Core Features",
      "priority": "critical",
      "tests": []
    }
  }
}
```

### 2. Add Test Cases

```json
{
  "id": "YOUR-001",
  "name": "Basic functionality",
  "description": "Test basic agent behavior",
  "input": {
    "param": "value"
  },
  "expect": {
    "status": "success",
    "has_fields": ["results"]
  }
}
```

### 3. Run Tests

```bash
/jarvis-test-runner --agent your-agent
```

## Best Practices

### Test Naming
- Use consistent ID prefixes (EXP, JNL, AUD, etc.)
- Sequential numbering within suites (001, 002, 003...)
- Descriptive but concise names

### Test Organization
- Group related tests in suites (core, filtering, security, etc.)
- Order by priority (critical tests first)
- One concern per test (don't test multiple features in one test)

### Assertions
- Be specific (don't just check `status: "success"`)
- Validate structure (check required fields exist)
- Validate values (check ranges, patterns, types)
- Use conditional assertions for optional features

### Maintenance
- Update tests when agent behavior changes
- Add regression tests for bugs
- Remove obsolete tests
- Keep test data realistic

## Example: Complete Test

```json
{
  "id": "EXP-010",
  "name": "Filter by entry type",
  "description": "Return only entries matching specified types",
  "input": {
    "entry_types": ["incident-log"],
    "limit": 5
  },
  "expect": {
    "status": "success",
    "results_max": 5,
    "all_match": {
      "path": "results[*].metadata.type",
      "value": "incident-log"
    },
    "has_fields": [
      "results",
      "summary",
      "pagination"
    ],
    "field_equals": {
      "pagination.offset": 0
    }
  }
}
```

## Troubleshooting

### Test Fails But Agent Works Manually
- Check assertion syntax
- Verify expected values match actual output
- Review agent output format changes

### Test Times Out
- Increase timeout in test runner config
- Check if agent is stuck
- Simplify test input

### Flaky Tests
- Add conditional assertions for optional features
- Check date/time dependencies
- Verify vault state assumptions

## Future Enhancements

- [ ] Parallel test execution
- [ ] Test data fixtures
- [ ] Mock vault for isolated testing
- [ ] Performance benchmarking
- [ ] Coverage tracking
- [ ] CI/CD integration
- [ ] Visual test reports (HTML)

## Contributing

When adding new agents:
1. Create test file: `tests/agents/your-agent.tests.json`
2. Write tests for all core features
3. Ensure critical tests pass
4. Document any special setup requirements

---

**Version:** 1.0.0
**Last Updated:** 2026-02-03
**Maintainer:** Jarvis Development Team
