---
name: jarvis-security-review
description: Run the Jarvis security agent for adversarial review. Use when user explicitly asks to "security review", "threat model", "review for vulnerabilities", "security audit", or "check for security issues".
---

# Skill: Security Review

**Trigger**: "security review", "threat model", "review for vulnerabilities", "security audit"

## Workflow

1. **Parse the request**: Identify the target (file paths, directory, PR number, or design doc) and optional mode (`THREAT-MODEL`, `CODE-REVIEW`, `CONFIG-AUDIT`, `CHANGE-REVIEW`).

2. **Delegate to `jarvis-security-agent`**: Spawn the agent with a prompt like:

   > Review [target] for security vulnerabilities. Mode: [mode or "auto-detect"].

   Include any additional context the user provided (scope constraints, specific concerns, threat actors).

3. **Return the report**: The agent produces a structured security report. Present it directly to the user.
