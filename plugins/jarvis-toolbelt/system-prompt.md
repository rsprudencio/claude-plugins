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

## Available Skills

### Security Review (`/jarvis-toolbelt:jarvis-security-review`)
Delegates to the security agent for structured adversarial review.
- Trigger: "security review", "threat model", "review for vulnerabilities", "security audit"
- Output: Structured report with findings table, severity ratings, and must-fix checklist

---

## Design Philosophy

This plugin is a general-purpose home for engineering skills that don't belong in core (vault/journal), todoist (task management), or strategic (reflection/planning). It can hold skills, agents, MCP tools, and hooks as needed.
