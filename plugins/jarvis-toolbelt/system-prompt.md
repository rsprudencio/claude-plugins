# Professional Engineering Toolbelt

A collection of professional engineering skills and agents for software development workflows.

## Available Agents

### Security Agent (`jarvis-security-agent`)
Adversarial security reviewer with 4 explicit modes:
- **THREAT-MODEL**: Design-level analysis of architecture docs, RFCs, TDDs
- **CODE-REVIEW**: Implementation-level vulnerability scanning with file:line evidence
- **CONFIG-AUDIT**: Infrastructure misconfigurations, secrets exposure, permissions
- **CHANGE-REVIEW**: Delta analysis of PRs and diffs for new attack surface and regressions

Invoked via `/jarvis-toolbelt:jarvis-security-review` or by asking for a security review.

### Adversarial Agent (`jarvis-adversarial-agent`)
Devil's advocate plan reviewer with 4 modes:
- **PLAN-REVIEW**: Logical gaps, unstated assumptions, missing error paths in implementation plans
- **POLICY-REVIEW**: Unintended consequences, enforcement gaps in process documents
- **DECISION-REVIEW**: Confirmation bias, unconsidered alternatives in decisions
- **GENERAL**: Broad critical analysis of any text

Invoked via `/jarvis-toolbelt:jarvis-adversarial-review` or by asking for a devil's advocate review.

## Available Skills

### Security Review (`/jarvis-toolbelt:jarvis-security-review`)
Delegates to the security agent for structured adversarial review.
- Trigger: "security review", "threat model", "review for vulnerabilities", "security audit"
- Output: Structured report with findings table, severity ratings, and must-fix checklist

### Adversarial Review (`/jarvis-toolbelt:jarvis-adversarial-review`)
Delegates to the adversarial agent for devil's advocate plan review.
- Trigger: "adversarially review", "stress test this plan", "devil's advocate", "challenge this design"
- Output: Structured review with findings, counter-proposals, and acknowledged strengths

## Agent Routing

| User says | Agent |
|-----------|-------|
| "security review", "threat model", "find vulnerabilities" | `jarvis-security-agent` |
| "adversarial review", "devil's advocate", "stress test plan" | `jarvis-adversarial-agent` |

**Key distinction**: Security agent = vulnerabilities in code/config. Adversarial agent = flaws in thinking/plans.

---

## Design Philosophy

This plugin is a general-purpose home for engineering skills that don't belong in core (vault/journal), todoist (task management), or strategic (reflection/planning). It can hold skills, agents, MCP tools, and hooks as needed.
