# Jarvis Strategic Plugin

**Version:** 1.0.0
**Author:** Raphael Prudencio
**License:** CC BY-NC 4.0

Strategic analysis and briefings for Jarvis AI Assistant.

---

## Features

- **Strategic Orientation:** Start-of-session briefings with context
- **Catch-up Summaries:** Cold-start briefings after time away
- **Journal Summarization:** Weekly/monthly activity summaries
- **Pattern Analysis:** Deep behavioral insights and trends

---

## Requirements

This plugin requires:

1. **Jarvis Core Plugin**
   ```bash
   claude plugin install jarvis@raph-claude-plugins
   ```

2. **Strategic memories** stored in vault at `.jarvis/strategic/`
   - No additional MCP servers required

---

## Installation

```bash
# Install core first
claude plugin install jarvis@raph-claude-plugins

# Then install Strategic extension
claude plugin install jarvis-strategic@raph-claude-plugins
```

---

## Usage

### Orient (Start of Session)

```
You: "/jarvis:jarvis-orient"
Jarvis: [Loads strategic context, shows priorities, current focus]
```

### Catchup (After Time Away)

```
You: "/jarvis:jarvis-catchup"
Jarvis: [Summarizes recent activity, refocuses on priorities]
```

### Summarize (Periodic Reflection)

```
You: "/jarvis:jarvis-summarize weekly"
Jarvis: [Analyzes journal entries, identifies patterns, creates summary]
```

### Patterns (Deep Analysis)

```
You: "/jarvis:jarvis-patterns"
Jarvis: [Analyzes behavioral patterns, updates strategic memories]
```

---

## Skills

All four strategic skills are included:

- `jarvis-orient` - Strategic briefing for work sessions
- `jarvis-catchup` - Cold start after absence
- `jarvis-summarize` - Periodic journal summaries
- `jarvis-patterns` - Behavioral analysis

---

## Support

- **Issues:** [GitHub Issues](https://github.com/rsprudencio/claude-plugins/issues)
- **Documentation:** See individual `skills/*/SKILL.md` files
