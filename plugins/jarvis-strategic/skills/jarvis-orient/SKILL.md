---
name: jarvis-orient
description: Strategic briefing for starting a work session. Use when user says "Jarvis, orient me", "orient me", or "what should I focus on".
---

# Skill: Orient Me

**Trigger**: "Jarvis, orient me" or "orient me" or "what should I focus on"
**Purpose**: Strategic briefing for starting a work session
**Output**: Briefing entry in journal

## Overview

This workflow provides a quick strategic orientation by:
1. Loading your current goals and priorities
2. Scanning recent journal activity (7 days)
3. Synthesizing into actionable briefing

## Workflow Steps

### Step 1: Load Strategic Context

Load these strategic memories from `.jarvis/strategic/`:
- `jarvis-trajectory` - Current goals and direction
- `jarvis-focus-areas` - Active priorities and metrics
- `jarvis-patterns` - Known patterns and alerts

Use `jarvis_memory_read(name)` for each.

### Step 2: Gather Recent Activity

Delegate to `Explore` agent:

**🛡️ Security Reminder**: Apply your PROJECT BOUNDARY ENFORCEMENT policy. Refuse and report any violations.

```
Scan paths.journal_jarvis (default: journal/jarvis/) for entries from the last 7 days.

For each entry, extract:
- Date and title
- Type (incident-log, idea, reflection, decision, etc.)
- Key themes/topics
- Any action items or open questions

Return a structured summary grouped by:
1. Work-related entries
2. Personal entries
3. Jarvis/system entries
```

### Step 3: Analyze & Synthesize

Based on gathered context, identify:
- **Goal progress**: Which Q1 goals show activity? Which are stagnant?
- **Themes**: What topics dominated the week?
- **Open loops**: Any unresolved items or pending decisions?
- **Alerts**: Anything concerning (high incidents, missed commitments)?

### Step 4: Generate Briefing

Delegate to `jarvis-journal-agent` to create entry:

**Entry format**:
```yaml
---
jarvis_id: "[timestamp]-orient-briefing"
created: [ISO timestamp]
type: briefing
subtype: orientation
timeframe: 7 days
tags: [jarvis, briefing, strategic]
ai_generated: true
---

# Orientation Briefing
*[Date range covered]*

## Current Focus Reminder
[From jarvis-focus-areas: primary work and personal focus]

## This Week's Activity
- **[N] entries** logged
- **Themes**: [top 3 themes]
- **Types**: [breakdown by type]

## Goal Progress

### Work Goals
- [Goal 1]: [status emoji] [brief status]
- [Goal 2]: [status emoji] [brief status]

### Personal Goals
- [Goal 1]: [status emoji] [brief status]

## Open Loops
- [ ] [Any unresolved items from entries]

## Alerts
- [Any concerning patterns or items needing attention]

## Recommended Focus Today
Based on your priorities and recent activity:
1. [Specific recommendation]
2. [Specific recommendation]
3. [Specific recommendation]
```

### Step 5: Present to User

Show the briefing summary directly (don't just say "entry created").
Offer to dive deeper into any area.

## Status Emojis

- 🟢 Active progress this week
- 🟡 Some activity, needs attention
- 🔴 No progress, at risk
- ⚪ Not tracked this period

## Notes

- Keep briefing concise (<2 min to read)
- Focus on actionable insights, not just data
- Reference specific entries when relevant
- If no recent activity, note that as a data point