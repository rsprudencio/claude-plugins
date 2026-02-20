---
name: jarvis-adversarial-review
description: Run the Jarvis adversarial agent for devil's advocate plan review. Use when user explicitly asks to "adversarially review", "stress test this plan", "devil's advocate", "challenge this design", or "poke holes in this".
---

# Skill: Adversarial Review

**Trigger**: "adversarially review", "stress test this plan", "devil's advocate", "challenge this design", "poke holes in this"

## Workflow

1. **Parse the request**: Identify the plan text (inline, file path, or from prior conversation context) and optional mode (`PLAN-REVIEW`, `POLICY-REVIEW`, `DECISION-REVIEW`, `GENERAL`).

2. **Delegate to `jarvis-adversarial-agent`**: Spawn the agent with a prompt like:

   > Adversarially review the following plan. Mode: [mode or "PLAN-REVIEW"].
   >
   > [plan text]

   Include any additional context the user provided (focus areas, assumptions, specific concerns). If the user asks for multi-round review (e.g., "3 rounds", "deep review", "thorough review"), include in the prompt: `Use the provider option "rounds": N`.

3. **Return the report**: The agent produces a structured adversarial review. Present it directly to the user.

## Routing Boundary

| User says | Route to |
|-----------|----------|
| "security review", "threat model", "find vulnerabilities" | `jarvis-security-agent` via `/jarvis-toolbelt:jarvis-security-review` |
| "adversarial review", "devil's advocate", "stress test plan" | `jarvis-adversarial-agent` via this skill |
