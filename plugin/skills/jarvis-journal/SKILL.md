---
name: jarvis-journal
description: Create journal entries with intelligent vault linking. Use when user says "Jarvis, journal this", "Jarvis, log this", "capture this", or "log this incident".
---

# Journal Entry Workflow

## Steps

1. **Clarify** type/context with user (AskUserQuestion):
   - Entry type: thought, incident-log, decision, learning, idea
   - Additional context if content is ambiguous

2. **Delegate** to `jarvis-journal-agent`:
   ```json
   {
     "mode": "create",
     "content": "user's content",
     "type": "detected or clarified type",
     "context": "any additional context",
     "clarifications": "user's answers to questions"
   }
   ```

3. **Present draft** to user for approval

4. **Handle response**:
   - **Approved** → Delegate commit to `jarvis-audit-agent`:
     ```json
     {
       "operation": "create",
       "description": "Journal entry: [brief summary]",
       "entry_id": "YYYYMMDDHHMMSS",
       "files": ["journal/jarvis/YYYY/MM/YYYYMMDDHHMMSS.md"]
     }
     ```
   - **Edit requested** → Collect feedback, re-delegate to agent with `mode: "edit"`
   - **Cancelled** → Delete the file, no commit (no git pollution)

## Agent Returns

```json
{
  "file_path": "journal/jarvis/2026/01/20260123143052.md",
  "entry_id": "20260123143052",
  "confidence": "high|medium|low",
  "tags": ["#type/thought", "#topic/jarvis"],
  "links": ["[[Related Note]]"]
}
```

## Entry Storage

Path: `journal/jarvis/YYYY/MM/YYYYMMDDHHMMSS.md`

## Important

- Agent writes the file but does NOT commit
- You handle commit only after user approval
- If user cancels, delete the file to keep git history clean
