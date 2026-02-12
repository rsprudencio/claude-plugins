# Markdown Format Reference

**Extension**: `.md`

## Metadata Block (YAML Frontmatter)

Delimited by `---` lines at the top of the file:

```yaml
---
jarvis_id: "YYYYMMDDHHMMSS-topic-slug"
created: YYYY-MM-DDTHH:MM:SSZ
modified: YYYY-MM-DDTHH:MM:SSZ
tags:
  - jarvis
  - type-tag
  - context-tag
type: note
sentiment: neutral
importance: medium
linked_to:
  - "[[Related Note]]"
ai_suggested: true
ai_confidence: 0.85
ai_generated: false
---
```

## Headings

Use `#` prefixed headings:

```markdown
# Title (H1)
## Section (H2)
### Subsection (H3)
```

## Links

Obsidian wiki-link syntax:

```markdown
[[Note Title]]
[[Note Title|Display Text]]
```

## Code Blocks

Fenced with triple backticks:

````markdown
```python
def example():
    pass
```
````

## Lists

```markdown
- Unordered item
- Another item

1. Ordered item
2. Another item
```

## Blockquotes

```markdown
> Quoted text here
```

## Journal Entry Template

```markdown
---
jarvis_id: "YYYYMMDDHHMMSS-topic-slug"
created: YYYY-MM-DDTHH:MM:SSZ
modified: YYYY-MM-DDTHH:MM:SSZ
tags:
  - jarvis
  - [type-tag]
  - [context-tag]
type: [entry-type]
sentiment: [sentiment]
importance: [level]
linked_to:
  - "[[Related Note]]"
ai_suggested: true
ai_confidence: 0.XX
ai_generated: false
---

# [Title]

## Original Input
> [Raw user input preserved exactly]

## Refined Entry

[Expanded version with context and connections]

### Context & Connections
- **Related to**: [[Link1]], [[Link2]]
- **Topics**: #tag1, #tag2
- **People**: Names mentioned
- **Systems**: Technical systems involved

---

**Jarvis Analysis:**
- Confidence: XX%
- Suggested N tags, M links
- Type: [detected type]
```
