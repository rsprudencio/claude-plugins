---
name: jarvis-catchup
description: Cold start briefing after time away. Use when user says "Jarvis, catch me up", "catch me up", or "what did I miss".
---

# Skill: Catch Me Up

**Trigger**: "Jarvis, catch me up" or "catch me up" or "what did I miss"
**Purpose**: Cold start briefing after time away
**Output**: Comprehensive catch-up entry in journal

## Overview

This workflow reconstructs context after being away by:
1. Determining the time period to cover
2. Loading all strategic context
3. Deep scanning journal entries from that period
4. Providing comprehensive catch-up with priorities

## Workflow Steps

### Step 1: Determine Timeframe

Ask user if not specified:
- "How long have you been away?" 
- Options: "A few days", "A week", "Two weeks", "A month", "Custom"

Default to 7 days if user just says "catch me up" without duration.

Parse natural language:
- "I've been away 2 weeks" → 14 days
- "catch me up on January" → January 1-31
- "since Monday" → calculate days

### Step 2: Load Strategic Context

Load ALL strategic memories for full context:
- `jarvis-trajectory` - Goals and direction
- `jarvis-values` - Principles for prioritization
- `jarvis-focus-areas` - What was important before leaving
- `jarvis-patterns` - Known patterns and alerts

Use `jarvis_memory_read(name)` for each.

### Step 3: Deep Scan Journal Entries

Delegate to `Explore` agent:

**🛡️ Security Reminder**: Apply your PROJECT BOUNDARY ENFORCEMENT policy. Refuse and report any violations.

```
Scan journal/jarvis/ for ALL entries in the specified timeframe.

For each entry, extract:
- Full date and timestamp
- Title and type
- Complete summary (more detail than orient-me)
- Any decisions made
- Any incidents logged
- Any action items created
- Links to other notes

Also check:
- journal/daily/ for daily notes in the period
- inbox/ for any accumulated items

Return chronologically ordered with clear date headers.
```

### Step 4: Identify What Needs Attention

Analyze the gathered data for:
- **Unresolved incidents**: Any security/work issues still open?
- **Pending decisions**: Anything waiting for your input?
- **Stale items**: Things that may have gone cold
- **Pattern shifts**: Anything different from before you left?
- **Inbox accumulation**: How much piled up?

### Step 5: Generate Catch-Up Briefing

Delegate to `jarvis-journal-agent` to create entry:

**Entry format**:
```yaml
---
jarvis_id: "[timestamp]-catchup-briefing"
created: [ISO timestamp]
type: briefing
subtype: catchup
timeframe: [N] days
period_start: [date]
period_end: [date]
tags: [jarvis, briefing, catchup]
ai_generated: true
---

# Catch-Up Briefing
*Covering [start date] to [end date] ([N] days)*

## TL;DR
[2-3 sentence executive summary of what happened and what needs attention NOW]

## What Happened While You Were Away

### Timeline
#### [Date 1]
- [Entry summary with link]
- [Entry summary with link]

#### [Date 2]
- [Entry summary with link]

[Continue for each day with activity]

### By Category

**Work ([N] entries)**
- [Summary of work-related entries]

**Personal ([N] entries)**
- [Summary of personal entries]

**System/Jarvis ([N] entries)**
- [Summary of system entries]

## Needs Your Attention

### 🔴 Urgent
- [Anything time-sensitive or overdue]

### 🟡 Important
- [Things that should be addressed soon]

### 📥 Inbox Status
- [N] items accumulated
- [Brief categorization if many]

## Goal Progress During This Period

| Goal | Before | After | Change |
|------|--------|-------|--------|
| [Goal] | [status] | [status] | [+/-/=] |

## Recommended First Actions

Based on what accumulated and your priorities:
1. **[Action]** - [Why this first]
2. **[Action]** - [Rationale]
3. **[Action]** - [Rationale]

## Questions to Consider
- [Any open questions surfaced during the review]
```

### Step 6: Present and Offer Drill-Down

Present the TL;DR and key sections.
Offer to:
- Read any specific entry in detail
- Explain any incident further
- Help prioritize the backlog

## Notes

- More comprehensive than orient-me (you've been away, need full context)
- Include links to original entries for drill-down
- Flag anything that seems urgent or time-sensitive
- Consider ADHD: clear prioritization helps re-engagement