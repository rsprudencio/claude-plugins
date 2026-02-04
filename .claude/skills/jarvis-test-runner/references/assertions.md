# Assertion Validation Reference

Complete reference for validating agent responses against test assertions. This guide is for Claude to follow when validating test results.

## Validation Approach

When validating a test response:

1. Read the `expect` object from the test case
2. For each assertion in `expect`, check if it passes against the response
3. Record which assertions failed and why
4. Test passes if ALL assertions pass

## Assertion Types

### Status Assertions

#### status
**Format:** `"status": "success"`

**Check:** Does response.status equal the expected value?

**Example:**
```json
Test expects: {"status": "success"}
Response has: {"status": "success"}
→ PASS

Response has: {"status": "error"}
→ FAIL: "Expected status 'success', got 'error'"
```

#### error_code
**Format:** `"error_code": "NO_FILTERS"`

**Check:** Does response.error_code equal the expected value?

---

### Content Assertions

#### contains
**Format:** `"contains": "filter"`

**Check:** When you convert the entire response to a string, does it contain this text?

**Use:** Quick check if response mentions something anywhere

**Example:**
```json
Test expects: {"contains": "security"}
Response: {"summary": "Found 3 security incidents"}
→ PASS (the word "security" appears in the response)
```

#### message_contains
**Format:** `"message_contains": "error"`

**Check:** Does response.message contain this text?

#### summary_contains
**Format:** `"summary_contains": "no results"`

**Check:** Does response.summary contain this text?

---

### Count Assertions

#### results_min
**Format:** `"results_min": 1`

**Check:** Does response.results array have at least N items?

**Example:**
```json
Test expects: {"results_min": 1}
Response: {"results": [{...}, {...}]}
→ PASS (2 results >= 1)

Response: {"results": []}
→ FAIL: "Expected at least 1 result, got 0"
```

#### results_max
**Format:** `"results_max": 10`

**Check:** Does response.results array have at most N items?

#### results_count
**Format:** `"results_count": 5`

**Check:** Does response.results array have exactly N items?

---

### Field Assertions

#### has_fields
**Format:** `"has_fields": ["results", "summary", "pagination.total"]`

**Check:** Do all these fields exist in the response? Use dot notation for nested fields.

**How to check:**
- `"results"` → Check if response.results exists
- `"pagination.total"` → Check if response.pagination.total exists (nested)
- `"metadata.created"` → Check if response.metadata.created exists

**Example:**
```json
Test expects: {"has_fields": ["results", "pagination.total"]}
Response: {"results": [...], "pagination": {"total": 10, "offset": 0}}
→ PASS (both fields exist)

Response: {"results": [...], "pagination": {"offset": 0}}
→ FAIL: "Missing field: pagination.total"
```

#### field_equals
**Format:** `"field_equals": {"pagination.offset": 0}`

**Check:** For each path → value pair, does the field at that path equal the expected value?

**Example:**
```json
Test expects: {"field_equals": {"pagination.offset": 0, "status": "success"}}
Response: {"pagination": {"offset": 0}, "status": "success"}
→ PASS

Response: {"pagination": {"offset": 5}, "status": "success"}
→ FAIL: "pagination.offset: expected 0, got 5"
```

#### field_contains
**Format:** `"field_contains": {"path": "summary", "text": "security"}`

**Check:** Does the field at the given path contain the text?

---

### Array Assertions

#### all_match
**Format:** `"all_match": {"path": "results[*].metadata.type", "value": "note"}`

**Check:** For every item in the array, does the specified field equal the value?

**Path notation:**
- `results[*].type` means: for each item in results, get its type field
- `results[*].metadata.tags` means: for each item in results, get its metadata.tags field

**Example:**
```json
Test expects: {"all_match": {"path": "results[*].type", "value": "note"}}
Response: {
  "results": [
    {"type": "note"},
    {"type": "note"}
  ]
}
→ PASS (all items have type: "note")

Response: {
  "results": [
    {"type": "note"},
    {"type": "incident-log"}
  ]
}
→ FAIL: "Found non-matching item: 'incident-log' at results[1].type"
```

#### all_match_one_of
**Format:** `"all_match_one_of": {"path": "results[*].type", "values": ["note", "incident-log"]}`

**Check:** For every item in the array, does the field match ONE of the allowed values?

#### all_match_pattern
**Format:** `"all_match_pattern": {"path": "results[*].file", "regex": "^journal/"}`

**Check:** For every item in the array, does the field match the regex pattern?

**Example:**
```json
Test expects: {"all_match_pattern": {"path": "results[*].file", "regex": "^journal/"}}
Response: {
  "results": [
    {"file": "journal/2026/01/note.md"},
    {"file": "journal/2026/02/entry.md"}
  ]
}
→ PASS (all files start with "journal/")

Response: {
  "results": [
    {"file": "journal/2026/01/note.md"},
    {"file": "work/document.md"}
  ]
}
→ FAIL: "File 'work/document.md' doesn't match pattern '^journal/'"
```

#### none_match_pattern
**Format:** `"none_match_pattern": {"path": "results[*].file", "regex": "^(people/|documents/)"}`

**Check:** For every item in the array, does the field NOT match the regex pattern?

**Use:** Verify sensitive directories are excluded

---

### Tag Assertions

#### all_have_tags
**Format:** `"all_have_tags": ["work", "security"]`

**Check:** Does every result have ALL of these tags in its metadata.tags array?

**Example:**
```json
Test expects: {"all_have_tags": ["work", "security"]}
Response: {
  "results": [
    {"metadata": {"tags": ["work", "security", "incident"]}},
    {"metadata": {"tags": ["work", "security"]}}
  ]
}
→ PASS (both results have both required tags)

Response: {
  "results": [
    {"metadata": {"tags": ["work"]}},  // missing "security"
    {"metadata": {"tags": ["work", "security"]}}
  ]
}
→ FAIL: "Result 0 missing required tag: 'security'"
```

#### none_have_tags
**Format:** `"none_have_tags": ["draft"]`

**Check:** Does NO result have ANY of these tags?

**Use:** Verify draft/excluded content is filtered out

#### each_has_any_tag
**Format:** `"each_has_any_tag": ["work", "personal"]`

**Check:** Does every result have AT LEAST ONE of these tags?

---

### Date Assertions

#### all_dates_between
**Format:**
```json
{
  "all_dates_between": {
    "path": "results[*].metadata.created",
    "start": "2026-01-01T00:00:00Z",
    "end": "2026-01-31T23:59:59Z"
  }
}
```

**Check:** Are all dates at the specified path between start and end (inclusive)?

**Example:**
```json
Test expects: dates between 2026-01-01 and 2026-01-31
Response: {
  "results": [
    {"metadata": {"created": "2026-01-15T10:00:00Z"}},
    {"metadata": {"created": "2026-01-28T14:30:00Z"}}
  ]
}
→ PASS (both dates in January 2026)

Response: {
  "results": [
    {"metadata": {"created": "2026-02-05T10:00:00Z"}}  // February!
  ]
}
→ FAIL: "Date 2026-02-05 is outside range 2026-01-01 to 2026-01-31"
```

#### dates_within_days
**Format:**
```json
{
  "dates_within_days": {
    "path": "results[*].metadata.created",
    "days": 7
  }
}
```

**Check:** Are all dates within N days of today?

---

### Range Assertions

#### all_in_range
**Format:**
```json
{
  "all_in_range": {
    "path": "results[*].relevance",
    "min": 0.0,
    "max": 1.0
  }
}
```

**Check:** Are all numeric values at the path between min and max (inclusive)?

**Example:**
```json
Test expects: {"all_in_range": {"path": "results[*].relevance", "min": 0.0, "max": 1.0}}
Response: {
  "results": [
    {"relevance": 0.95},
    {"relevance": 0.87},
    {"relevance": 1.0}
  ]
}
→ PASS (all values between 0.0 and 1.0)

Response: {
  "results": [
    {"relevance": 1.2}  // Out of range!
  ]
}
→ FAIL: "Value 1.2 is outside range 0.0-1.0"
```

#### range_check
**Format:**
```json
{
  "range_check": {
    "path": "performance.search_duration_ms",
    "min": 0,
    "max": 5000
  }
}
```

**Check:** Is the single value at this path between min and max?

**Use:** Verify performance metrics are reasonable

---

### Special Assertions

#### sorted_desc
**Format:** `"sorted_desc": {"path": "results[*].relevance"}`

**Check:** Are the values at this path sorted in descending order (highest to lowest)?

**Example:**
```json
Test expects: {"sorted_desc": {"path": "results[*].relevance"}}
Response: {
  "results": [
    {"relevance": 1.0},
    {"relevance": 0.9},
    {"relevance": 0.85}
  ]
}
→ PASS (1.0 > 0.9 > 0.85, descending order)

Response: {
  "results": [
    {"relevance": 0.9},
    {"relevance": 1.0},  // Out of order!
    {"relevance": 0.85}
  ]
}
→ FAIL: "Values not sorted descending at index 1"
```

#### array_contains
**Format:**
```json
{
  "array_contains": {
    "path": "sensitive_dirs_skipped",
    "values": ["people/", "documents/"]
  }
}
```

**Check:** Does the array at this path contain ALL of these values?

---

### Conditional Assertions

#### one_of
**Format:**
```json
{
  "one_of": [
    {"status": "error"},
    {"contains": "filter"}
  ]
}
```

**Check:** Does AT LEAST ONE of these sub-assertions pass?

**Use:** When either condition A OR condition B is acceptable

**Example:**
```json
Test expects: {"one_of": [{"status": "error"}, {"contains": "filter"}]}
Response: {"status": "success", "message": "Please provide a filter"}
→ PASS (second assertion passes: contains "filter")

Response: {"status": "success", "message": "Done"}
→ FAIL: "Neither status is 'error' nor response contains 'filter'"
```

#### if_results
**Format:**
```json
{
  "if_results": {
    "all_have_tags": ["work"]
  }
}
```

**Check:** IF response.results exists and is non-empty, THEN check these assertions. Otherwise pass.

**Use:** Conditional validation - only check tags if results were found

**Example:**
```json
Test expects: {"if_results": {"all_have_tags": ["work"]}}
Response: {"results": []}
→ PASS (no results, so skip the tag check)

Response: {"results": [{"metadata": {"tags": ["work"]}}]}
→ PASS (has results, and they have "work" tag)

Response: {"results": [{"metadata": {"tags": ["personal"]}}]}
→ FAIL: "Result missing required tag: 'work'"
```

#### if_has_history
**Format:**
```json
{
  "if_has_history": {
    "all_match": {"path": "history_results[*].operation", "value": "delete"}
  }
}
```

**Check:** IF response.history_results exists, THEN check these assertions. Otherwise pass.

#### if_sensitive_results
**Format:**
```json
{
  "if_sensitive_results": {
    "has_field": "sensitive",
    "value": true
  }
}
```

**Check:** IF any result file path starts with "people/" or "documents/", THEN check these assertions.

---

## Validation Workflow

When validating a test:

### Step 1: Load Test Expectations

Read the `expect` object from the test case.

### Step 2: Check Each Assertion

For each assertion in `expect`:
1. Determine the assertion type
2. Follow the validation rules above
3. Record pass/fail and failure message

### Step 3: Determine Overall Result

- Test PASSES if ALL assertions pass
- Test FAILS if ANY assertion fails

### Step 4: Generate Failure Report

For failed assertions, include:
- Assertion type
- Expected value
- Actual value
- Clear explanation of why it failed

## Example Validation

**Test case:**
```json
{
  "input": {"search_text": "security", "limit": 5},
  "expect": {
    "status": "success",
    "results_max": 5,
    "all_match_pattern": {"path": "results[*].file", "regex": "^journal/"}
  }
}
```

**Agent response:**
```json
{
  "status": "success",
  "results": [
    {"file": "journal/2026/01/incident.md"},
    {"file": "journal/2026/01/review.md"},
    {"file": "work/security-doc.md"}
  ]
}
```

**Validation:**

1. ✅ `status: "success"` - Response status is "success"
2. ✅ `results_max: 5` - 3 results <= 5
3. ❌ `all_match_pattern` - FAIL
   - Expected: All files match `^journal/`
   - Actual: File "work/security-doc.md" doesn't match pattern
   - Message: "File 'work/security-doc.md' at results[2] doesn't match pattern '^journal/'"

**Result:** Test FAILS (1 of 3 assertions failed)

## Tips for Validation

### Be Precise

- Check exact equality where specified
- Don't be lenient with type mismatches
- Follow the assertion rules exactly

### Provide Clear Failures

- State what was expected
- State what was actually found
- Explain why it doesn't match

### Handle Missing Fields

- If a field doesn't exist, that's usually a failure
- Unless it's a conditional assertion (if_results, etc.)

### Array Path Notation

When you see `results[*].field`:
1. Get the results array
2. For each item, extract the field
3. Check the assertion against all extracted values

Example: `results[*].metadata.type`
- From `[{metadata: {type: "note"}}, {metadata: {type: "log"}}]`
- Extract: `["note", "log"]`
- Check assertion against this array
