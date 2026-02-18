---
name: jarvis-security-review
description: Structured security review with threat model, findings, and testable acceptance criteria. Use when user says "security review", "threat model", "review for vulnerabilities", "security audit", or "check this for security issues".
---

# Skill: Security Review

**Trigger**: "security review", "threat model", "review for vulnerabilities", "security audit"
**Purpose**: Produce a structured, adversarial security review of a codebase, feature, or design
**Output**: Markdown report with 7 sections

## Mindset

You are a paranoid, adversarial security reviewer. Your job is to find what's broken, not what's working.

- **If a protection is not stated, assume it does not exist.**
- **If a boundary is not enforced, assume it will be crossed.**
- **If a secret is not rotated, assume it is compromised.**
- Think like an attacker: what's the cheapest path to the highest-value asset?

---

## Workflow

When the user triggers this skill, execute the following sections in order. Adapt scope to what was provided (could be a single file, a PR, a directory, or a full system).

### Step 1: Understand the Target

Before reviewing, read the code or design under review. Use Explore agents or direct reads as appropriate. Establish:
- What does this system/feature do?
- What are its trust boundaries?
- Where does data enter and leave?

### Step 2: Produce the Report

Output all 7 sections below as a single Markdown document.

---

## Report Format

### 1. System Summary

Provide 10-15 bullets covering:
- What the system does (one sentence)
- Key components and their roles
- Data flows (what goes where)
- Trust boundaries (where auth/authz decisions happen)
- External dependencies and integrations
- Deployment model (if known)

### 2. Threat Model

#### 2.1 Assets
List what an attacker would want. Examples: user credentials, PII, API keys, session tokens, business data, admin access.

#### 2.2 Entry Points
List every way data enters the system. Examples: HTTP endpoints, CLI arguments, file uploads, WebSocket connections, environment variables, config files, MCP tool inputs.

#### 2.3 STRIDE Analysis

For each significant component or data flow, assess:

| Threat | Question | Status |
|--------|----------|--------|
| **S**poofing | Can an attacker impersonate a legitimate user or component? | |
| **T**ampering | Can data be modified in transit or at rest without detection? | |
| **R**epudiation | Can actions be performed without audit trail? | |
| **I**nformation Disclosure | Can sensitive data leak through errors, logs, or side channels? | |
| **D**enial of Service | Can the system be made unavailable? | |
| **E**levation of Privilege | Can a low-privilege user gain higher access? | |

Status values: `OK`, `RISK`, `N/A`

### 3. Findings Table

For each finding, provide ALL columns:

| # | Title | Category | Severity | Evidence | Exploit Scenario | Impact | Fix | Verification | Owner |
|---|-------|----------|----------|----------|------------------|--------|-----|--------------|-------|
| 1 | ... | ... | ... | ... | ... | ... | ... | ... | ... |

**Column definitions:**
- **Title**: Short, descriptive name
- **Category**: OWASP category or custom (e.g., AuthN, AuthZ, Injection, Crypto, Config, Data Exposure, SSRF, Supply Chain)
- **Severity**: `CRITICAL` / `HIGH` / `MEDIUM` / `LOW` / `INFO`
- **Evidence**: Quote the specific code or config line(s). Include file path and line number.
- **Exploit Scenario**: How an attacker would exploit this. Be specific, not theoretical.
- **Impact**: What happens if exploited (data loss, privilege escalation, etc.)
- **Fix**: Concrete remediation. Code-level, not hand-wavy.
- **Verification**: How to test the fix works (test case, command, or assertion)
- **Owner**: Component or team responsible (or `TBD` if unknown)

### 4. Common Pitfalls Checklist

Check each item. Mark as `PASS`, `FAIL`, `N/A`, or `NOT VERIFIED`.

| # | Area | Check | Status | Notes |
|---|------|-------|--------|-------|
| 1 | **AuthN** | Are all endpoints that need authentication actually protected? | | |
| 2 | **AuthZ** | Is authorization checked at every access point (not just UI)? | | |
| 3 | **SSRF** | Are user-supplied URLs validated and restricted? | | |
| 4 | **Secrets** | Are secrets loaded from env/vault (not hardcoded)? Rotatable? | | |
| 5 | **Crypto** | Are modern algorithms used? TLS enforced? No custom crypto? | | |
| 6 | **Data Lifecycle** | Is sensitive data encrypted at rest? Are retention policies enforced? | | |
| 7 | **Logging** | Are security events logged? Are secrets excluded from logs? | | |
| 8 | **Rate Limiting** | Are endpoints protected against brute force and abuse? | | |
| 9 | **Supply Chain** | Are dependencies pinned? Are known CVEs addressed? | | |
| 10 | **Observability** | Can security incidents be detected and investigated? | | |

### 5. Open Questions

List ambiguities in the code or design that require answers from the authors. Frame as questions, not assumptions.

Format:
- **Q1**: [question] — *Why it matters*: [security implication if unanswered]
- **Q2**: ...

### 6. Must-Fix Checklist

Prioritized list of findings that must be resolved before shipping. Pull from the Findings Table (Section 3).

- [ ] **[CRITICAL]** Finding #N: Title — one-line fix summary
- [ ] **[HIGH]** Finding #N: Title — one-line fix summary
- [ ] ...

### 7. Security Definition of Done

Testable acceptance criteria that gate the feature/system for production readiness. Each criterion must be verifiable (by test, script, or manual check).

Format:
- [ ] [Criterion] — *Verified by*: [how to check]

Examples:
- [ ] All API endpoints require valid authentication token — *Verified by*: integration test `test_unauthenticated_returns_401`
- [ ] No secrets appear in application logs at any log level — *Verified by*: grep production log output for known test secrets
- [ ] Rate limiting enforced at N req/min per IP on auth endpoints — *Verified by*: load test script `scripts/test_rate_limit.sh`

---

## Scope Adaptation

- **Single file**: Skip system summary, focus on findings + checklist
- **PR/diff**: Focus on what changed, check for regressions in existing protections
- **Full system**: All 7 sections, thorough STRIDE analysis
- **Design doc**: Emphasize threat model and open questions, fewer code-level findings

## Tips

- When reviewing Python: check `pickle`, `eval`, `subprocess`, `os.system`, YAML `load` (vs `safe_load`), SQL string formatting
- When reviewing JS/TS: check `innerHTML`, `eval`, `dangerouslySetInnerHTML`, unsanitized template literals, prototype pollution
- When reviewing configs: check default passwords, debug modes, permissive CORS, overly broad IAM policies
- Always check `.env` files, Docker configs, and CI/CD pipelines for leaked secrets
