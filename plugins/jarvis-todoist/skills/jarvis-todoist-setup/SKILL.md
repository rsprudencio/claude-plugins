---
name: jarvis-todoist-setup
description: Interactive setup wizard for Todoist routing rules. Use when user says "setup todoist routing", "configure todoist", or "jarvis-todoist-setup".
---

# Jarvis Todoist Setup Wizard

**Purpose**: Generate personalized `todoist-routing-rules` through analysis or guided interview.

---

## Step 1: Choose Setup Mode

Ask using AskUserQuestion:

```
header: "Setup mode"
question: "How would you like to configure Todoist routing?"
options:
  - label: "Analyze my Todoist (Recommended)"
    description: "I'll look at your projects and tasks, then suggest routing rules"
  - label: "I'll define my own rules"
    description: "Walk me through creating custom categories manually"
  - label: "Skip setup for now"
    description: "Use basic sync without custom routing"
  - label: "Wait, explain what this does first"
    description: "Tell me more about Todoist routing before I decide"
```

**If "Wait, explain what this does first"**:

Explain conversationally:

```
## What Todoist Routing Does

When you dump tasks into Todoist inbox, this sync helps organize them:

**Example:** You add "Buy groceries" and "Had a great idea about my side project" to your inbox.

**Without routing:**
- Both stay in inbox, you organize manually

**With routing:**
- "Buy groceries" → Recognized as a clear task, stays in Todoist with a label
- "Had a great idea..." → Looks like a thought/reflection, gets captured to your vault inbox for later review
- Tasks mentioning "side project" → Could auto-route to a "Side Project" Todoist project

**Memory sync (optional):**
- Side project ideas can also be added to a memory file, so Jarvis remembers them in future sessions

Make sense? Ready to set up your routing?
```

Then re-ask the setup mode question (without the "explain" option).

**If "Skip setup for now"**: Confirm and exit - no memory created, basic sync applies.

**If "I'll define my own rules"**: Jump to [Manual Setup Flow](#manual-setup-flow).

**If "Analyze my Todoist"**: Continue to Step 2.

---

## Step 2: Gather Data for Analysis

Fetch in parallel:

1. **User info**: `mcp__plugin_jarvis-todoist_api__user_info` - Get plan type (Free/Pro/Business)
2. **Projects**: `mcp__plugin_jarvis-todoist_api__find_projects` - Get all existing projects
3. **Inbox tasks**: `mcp__plugin_jarvis-todoist_api__find_tasks` with `projectId: "inbox"`, `limit: 100`
4. **Recent tasks from all projects**: `mcp__plugin_jarvis-todoist_api__find_tasks_by_date` with `startDate: "today"`, `daysCount: 30`

### Project Limit Check

**Todoist plan limits:**
- Free: 5 active projects max
- Pro/Business: 300 active projects max

Calculate: `available_slots = limit - current_project_count`

If `available_slots <= 0`:
- Warn user: "You've reached your Todoist project limit (5 for Free plan). I'll route to existing projects only."
- Skip any recommendations that would create new projects
- Suggest upgrading or archiving unused projects

---

## Step 3: Analyze Patterns

Analyze the fetched data for:

### Project Analysis
- What projects exist? (names, task counts)
- Are there obvious categories? (Work, Personal, specific projects)
- Any empty/unused projects?

### Task Content Analysis
Look for keyword clusters in unclassified inbox items:

| Pattern | Likely Category | Example Keywords |
|---------|-----------------|------------------|
| Work/professional | WORK | meeting, review, deadline, client, project name |
| Errands/chores | ERRANDS | buy, pick up, call, schedule, appointment |
| Learning/growth | LEARNING | read, watch, learn, course, book |
| Health/fitness | HEALTH | gym, run, workout, doctor, meal prep |
| Side project | PROJECT | specific product/project names that repeat |
| Reflection/ideas | CAPTURE | "what if", "idea:", "thought:", realized |

### Routing Opportunities
- Tasks mentioning existing project names → route to that project
- Clusters of related keywords → suggest new project
- Ambiguous items → recommend inbox capture

---

## Step 4: Present Analysis & Proposals

Present findings to user:

```markdown
## Todoist Analysis Complete

**Your current setup:**
- 3 projects: Inbox, Work, Personal
- 45 inbox tasks (32 unclassified)

**Patterns I detected:**

| Category | Tasks | Keywords Found | Suggested Action |
|----------|-------|----------------|------------------|
| Work-related | 12 | "meeting", "review", "standup" | → Route to "Work" project |
| Errands | 8 | "buy", "pick up", "groceries" | → Keep in Inbox or create "Errands" |
| Side project "MyApp" | 5 | "myapp", "feature", "bug" | → Create "MyApp" project + memory sync |
| Reflections | 4 | "realized", "idea" | → Capture to inbox/ |
| Unclear | 3 | Mixed signals | → Default handling |

**My recommendations:**

1. **Create routing rule: WORK**
   - Keywords: `meeting`, `review`, `standup`, `deadline`
   - Route to: Work project

2. **Create routing rule: MYAPP** (your side project)
   - Keywords: `myapp`, `feature`, `bug`, `idea for myapp`
   - Route to: New "MyApp" project
   - Memory sync: `myapp-roadmap` (track ideas over time)

3. **Keep default for everything else**

Would you like me to apply these recommendations?
```

---

## Step 5: User Approval & Refinement

Ask using AskUserQuestion:

```
header: "Apply rules"
question: "How should I proceed with these recommendations?"
options:
  - label: "Apply all recommendations"
    description: "Create the routing rules as suggested"
  - label: "Let me adjust first"
    description: "I'll modify some categories before applying"
  - label: "Start over manually"
    description: "Ignore analysis, let me define from scratch"
```

**If "Let me adjust"**:
- Present each recommendation individually
- Allow user to: approve, modify keywords, change target project, skip
- For side projects: ask if they want memory sync

**If "Apply all"**: Continue to Step 6.

---

## Step 6: Create Missing Projects (If Needed)

**First, check project limit from Step 2:**

```
current_projects = [count from find-projects]
plan_limit = 5 if Free else 300
available_slots = plan_limit - current_projects
projects_to_create = [count of new projects in recommendations]
```

**If `projects_to_create > available_slots`:**

```markdown
⚠️ **Project Limit Warning**

You're on Todoist Free (5 project limit).
- Current projects: 4
- Available slots: 1
- Recommendations need: 2 new projects

I can only create 1 new project. Options:
1. Pick which project to create (route others to Inbox)
2. Route all to existing projects
3. Archive an existing project to free up a slot
```

**If slots available**, ask:

```
header: "New projects"
question: "I'll create these new Todoist projects:"
options:
  - label: "Create all"
    description: "MyApp, Errands (2 new projects) - You have 3 slots available"
  - label: "Let me choose"
    description: "I'll pick which ones to create"
  - label: "Skip project creation"
    description: "Route to Inbox instead"
```

Use `mcp__plugin_jarvis-todoist_api__add_projects` to create approved projects.

---

## Step 7: Generate Rules Memory

Build the `todoist-routing-rules` strategic memory file from approved recommendations. Write to `.jarvis/strategic/todoist-routing-rules.md` via `jarvis_store(type="memory", name="todoist-routing-rules", content=content)`.

**Format**: YAML for reliable parsing + human editability.

```yaml
# Todoist Routing Rules
# Generated: [Current date]
# Edit this file directly or run /jarvis-todoist-setup to regenerate

version: 1
default_behavior: smart  # smart | all_todoist | all_vault

classifications:
  # Checked in order - first match wins

  - name: WORK
    description: Work and professional tasks
    keywords:
      - meeting
      - review
      - standup
      - deadline
      - client
    labels:
      - work
      - jarvis-ingested
    project: Work
    action: keep  # keep | capture

  - name: MYAPP
    description: Side project ideas and tasks
    keywords:
      - myapp
      - feature
      - bug
      - "idea for myapp"
    labels:
      - myapp
      - jarvis-ingested
    project: MyApp
    action: keep
    memory_sync:
      memory: myapp-roadmap
      section: Ideas

# Default behavior options:
#   smart: Clear tasks stay in Todoist, reflections go to vault inbox
#   all_todoist: Everything stays in Todoist
#   all_vault: Everything captured to vault inbox
```

**Schema Notes** (for LLM parsing):
- `classifications[]`: Array, checked in order
- `keywords[]`: Case-insensitive substring match
- `labels[]`: Applied to matching tasks
- `project`: Target Todoist project (must exist)
- `action`: `keep` (stays in Todoist) or `capture` (goes to vault inbox)
- `memory_sync`: Optional, appends to specified memory/section

---

## Step 8: Confirm & Next Steps

```markdown
## Setup Complete!

**Created:**
- Routing rules memory with 2 custom categories
- 1 new Todoist project: "MyApp"

**Your routing:**
| Pattern | Action |
|---------|--------|
| Work keywords | → Work project |
| MyApp keywords | → MyApp project + memory sync |
| Clear tasks | → Stay in Inbox |
| Reflections/ideas | → Captured to vault |

**Next steps:**
- Run `/jarvis-todoist` to sync with new rules
- Run `/jarvis-todoist-setup` anytime to modify

Want to run a sync now to test the new rules?
```

---

## Manual Setup Flow

If user chose "I'll define my own rules":

### M1: Ask About Categories

```
header: "Categories"
question: "What categories of tasks do you want to route automatically?"
options:
  - label: "Work vs Personal"
    description: "Separate professional and personal tasks"
  - label: "By project"
    description: "Route to specific projects based on keywords"
  - label: "Side project tracking"
    description: "Track ideas for a specific project with memory sync"
  - label: "Custom categories"
    description: "I'll name my own categories"
multiSelect: true
```

### M2: For Each Category

Collect:
- Category name
- Keywords (comma-separated)
- Target project (existing or new)
- Memory sync? (yes/no, if yes which memory)

### M3: Default Behavior

```
header: "Unmatched items"
question: "When a task doesn't match any of your categories, what should happen?"
options:
  - label: "Smart sorting (Recommended)"
    description: "Action items like 'Buy milk' stay in Todoist. Thoughts like 'I realized...' go to your vault inbox."
  - label: "Everything stays in Todoist"
    description: "I'll organize manually - don't move anything to the vault"
  - label: "Everything to vault inbox"
    description: "Capture all to my vault inbox so I can review and decide later"
```

### M4: Generate & Confirm

Same as Step 7-8.

---

## Error Handling

| Error | Action |
|-------|--------|
| Strategic memory dir missing | Warn: ".jarvis/strategic/ not found. Run /jarvis-settings to initialize. Continue anyway?" |
| Todoist MCP unavailable | Cannot proceed - required for analysis |
| No inbox tasks | "Your inbox is empty! Add some tasks first, or define rules manually." |
| Project creation fails | Report error, offer to route to Inbox instead |
| Project limit reached (Free: 5) | Route to existing projects only, suggest archiving unused or upgrading |
| Would exceed project limit | Only create projects up to available slots, route remainder to Inbox |

---

## Notes

- Analysis mode is recommended for new users
- Manual mode for power users who know what they want
- Rules can always be edited directly in the memory
- Re-running setup offers to modify or replace existing rules
