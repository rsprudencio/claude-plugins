---
name: jarvis-patterns
description: Deep behavioral analysis and strategic memory maintenance. Use when user says "Jarvis, analyze patterns", "what patterns do you see", or "pattern analysis".
---

# Skill: Analyze Patterns

**Trigger**: "Jarvis, analyze patterns" or "what patterns do you see" or "pattern analysis"
**Purpose**: Deep behavioral analysis and strategic memory maintenance
**Output**: Analysis entry + suggestions for patterns memory update

## Overview

This is the deepest analysis workflow:
1. Comprehensive scan of journal entries
2. Cross-reference with strategic memories
3. Identify behavioral patterns and trends
4. Generate insights and recommendations
5. Suggest updates to jarvis-patterns memory

## Workflow Steps

### Step 1: Determine Scope

Parse timeframe from user request:
- "from last week" → 7 days
- "from January" → that month
- "from Q1" → quarter
- "all time" → everything available

Default: 30 days (meaningful pattern detection requires time)

### Step 2: Load All Strategic Context

Load ALL strategic memories from `.jarvis/strategic/`:
- `jarvis-trajectory` - Goals to check progress against
- `jarvis-values` - Principles to check alignment
- `jarvis-focus-areas` - Priorities to compare activity
- `jarvis-patterns` - Previous patterns to compare/update

Use `jarvis_memory_read(name)` for each.

### Step 3: Deep Journal Analysis

Delegate to `Explore` agent:

**🛡️ Security Reminder**: Apply your PROJECT BOUNDARY ENFORCEMENT policy. Refuse and report any violations.

```
Comprehensive scan of paths.journal_jarvis (default: journal/jarvis/) for [timeframe].

**Quantitative Analysis:**
- Entry frequency by day/week
- Type distribution over time
- Tag frequency and co-occurrence
- Link density (how connected are entries)
- Time-of-day patterns (when entries are created)

**Qualitative Analysis:**
- Theme extraction (NLP-style topic clustering)
- Sentiment progression over time
- Decision patterns (what triggers decisions)
- Problem types (recurring issues)
- Idea categories (what sparks creativity)

**Goal Alignment:**
- Which goals have supporting entries?
- Which goals have no activity?
- Any goal drift (working on things not in goals)?

**Values Alignment:**
- Entries showing values in action
- Any potential values conflicts
- Decision rationale patterns

**ADHD Pattern Detection:**
- Dropped threads (started but no follow-up)
- Hyperfocus indicators (burst of activity on one topic)
- Energy patterns (productive vs low periods)
- Context switch frequency
```

### Step 4: Generate Insights

Based on analysis, identify:

**Positive Patterns** ✓
- What's working well
- Consistent behaviors
- Growth areas

**Concerning Patterns** ⚠️
- Dropped items (ADHD alert)
- Goal misalignment
- Values conflicts
- Negative sentiment trends

**Opportunities** 💡
- Underexplored areas
- Connection possibilities
- Efficiency improvements

**Recommendations** 📋
- Specific actions to take
- Habits to reinforce
- Things to stop doing

### Step 5: Generate Analysis Entry

Delegate to `jarvis-journal-agent`:

**Entry format**:
```yaml
---
jarvis_id: "[timestamp]-pattern-analysis"
created: [ISO timestamp]
type: analysis
subtype: patterns
timeframe: "[period]"
entry_count_analyzed: [N]
tags: [jarvis, analysis, patterns, strategic]
ai_generated: true
---

# Pattern Analysis
*Analyzing [N] entries from [period]*

## Executive Summary
[3-4 sentences on key findings]

## Quantitative Overview

### Activity Patterns
| Metric | Value | Trend |
|--------|-------|-------|
| Entries/week | [N] | [↑↓→] |
| Most active day | [day] | - |
| Peak hours | [time range] | - |
| Avg response time | [for incidents] | - |

### Type Distribution
[Chart or breakdown showing entry types over time]

### Theme Clusters
1. **[Theme]** - [N] entries, [trend]
2. **[Theme]** - [N] entries, [trend]
3. **[Theme]** - [N] entries, [trend]

## Goal Progress Analysis

### Active Goals (from jarvis-trajectory)

| Goal | Evidence | Status | Recommendation |
|------|----------|--------|----------------|
| VMP Independence | [N] entries | 🟢 | [action] |
| Architecture Deep Dive | [N] entries | 🟡 | [action] |
| Exercise Routine | [N] entries | 🔴 | [action] |
| Prayer/Scripture | [N] entries | ⚪ | [action] |

### Goal Drift Detection
- **On track**: [goals with activity]
- **Drifting**: [goals without activity]
- **Unplanned work**: [activity not tied to goals]

## Values Alignment Check

### Values in Action
- **Care to Challenge**: [examples from entries]
- **Team over Self**: [examples]
- **Faith Framework**: [examples]

### Potential Tensions
- [Any entries showing values conflicts]

## ADHD Pattern Analysis

### Dropped Threads 🔴
Items started but not followed up:
- [Item] - last mentioned [date]
- [Item] - last mentioned [date]

### Hyperfocus Episodes
- [Topic] - [N] entries in [short period]

### Energy Patterns
- High productivity: [days/times]
- Low periods: [days/times]
- Recovery patterns: [observations]

### Context Switch Analysis
- Avg topics per day: [N]
- Deep work sessions: [frequency]

## Behavioral Insights

### Positive Patterns ✓
1. [Pattern] - Evidence: [entries]
2. [Pattern] - Evidence: [entries]

### Concerning Patterns ⚠️
1. [Pattern] - Risk: [what could happen]
2. [Pattern] - Risk: [what could happen]

### Opportunities 💡
1. [Opportunity] - How to leverage
2. [Opportunity] - How to leverage

## Strategic Recommendations

### Immediate (This Week)
1. [Specific action]
2. [Specific action]

### Short-term (This Month)
1. [Action]
2. [Action]

### Habit Suggestions
- **Start**: [new habit to adopt]
- **Stop**: [habit to break]
- **Continue**: [habit to reinforce]

## Suggested Memory Updates

Based on this analysis, I recommend updating `jarvis-patterns`:

### Add to Detected Themes
```
[New theme data to add]
```

### Update Goal Progress
```
[Updated goal tracking]
```

### New Alerts
```
[Any alerts to add]
```

---
*Deep analysis by Jarvis patterns workflow*
```

### Step 6: Offer Memory Updates

Present the suggested updates to `jarvis-patterns` memory.
Ask user: "Would you like me to update the patterns memory with these findings?"

If approved, read the current `jarvis-patterns` memory with `jarvis_memory_read("jarvis-patterns")`, merge the new findings into the content, then write back with `jarvis_memory_write("jarvis-patterns", updated_content, overwrite=true)`.

### Step 7: Discuss Findings

Offer to:
- Explain any pattern in more detail
- Discuss recommendations
- Create action items from insights
- Schedule follow-up analysis

## Analysis Depth Levels

**Quick** (triggered by "quick pattern check"):
- Last 7 days only
- Basic stats
- Top 3 findings

**Standard** (default):
- 30 days
- Full analysis
- All sections

**Deep** (triggered by "deep analysis" or "comprehensive"):
- All available history
- Trend comparisons
- Predictive insights

---

## Mid-Session Focus Check

**Trigger**: "Jarvis, what threads are open?" or "focus check" or "what am I working on?"

This is a **point-in-time analysis**, not live monitoring. It summarizes the current session on demand.

### Workflow

1. **Summarize conversation threads**:
   - Topics discussed in the current session
   - Status of each: active, dormant (mentioned early but not revisited), concluded
   - Any pending decisions or open questions

2. **Surface pending items**:
   - Unfinished tasks mentioned during session
   - Items the user said they'd come back to
   - Topics that drifted without resolution

3. **Suggest next steps**:
   - Close dormant threads via journal capture ("Want me to journal that thought about X?")
   - Set a Todoist reminder for items that need follow-up
   - Refocus on the primary task if drift is detected

### Example Output

```
## Focus Check

**Active threads:**
1. Scheduling implementation (primary) — in progress
2. Roadmap memory update — pending

**Dormant threads:**
3. Shell integration testing — mentioned at start, not revisited

**Suggested actions:**
- Continue with scheduling (primary focus)
- Roadmap update can happen after implementation
- Want me to create a reminder for shell testing?
```

### Important

- This is **always available on demand** — no config flag needed
- It reads conversation context only, no vault/API queries required
- Keep it lightweight — this should take seconds, not minutes

---

## Notes

- Historical pattern analysis is the most resource-intensive workflow
- Results should inform strategic planning
- Run monthly at minimum for value
- Patterns memory gets smarter over time
- ADHD patterns are critical - surface them clearly
- Mid-session focus check is lightweight and always available