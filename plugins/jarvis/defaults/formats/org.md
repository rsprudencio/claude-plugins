# Org-mode Format Reference

**Extension**: `.org`

## Metadata Block (Property Drawer)

Uses `:PROPERTIES:` drawer at the start of the file:

```org
:PROPERTIES:
:JARVIS_ID: YYYYMMDDHHMMSS-topic-slug
:CREATED: YYYY-MM-DDTHH:MM:SSZ
:MODIFIED: YYYY-MM-DDTHH:MM:SSZ
:TYPE: note
:SENTIMENT: neutral
:IMPORTANCE: medium
:AI_SUGGESTED: true
:AI_CONFIDENCE: 0.85
:AI_GENERATED: false
:END:
#+TAGS: jarvis type-tag context-tag
```

**Notes:**
- Property names are UPPERCASE by Org convention
- Tags go in a `#+TAGS:` keyword line (space-separated), not inside the drawer
- Linked notes use `#+LINKED_TO:` keyword line

## Headings

Use `*` prefixed headings (number of stars = level):

```org
* Title (Level 1)
** Section (Level 2)
*** Subsection (Level 3)
```

## Links

Org-mode link syntax:

```org
[[file:Related Note.org][Related Note]]
```

For internal vault links (Obsidian-style, also works in Org):

```org
[[Related Note]]
```

## Code Blocks

Delimited by `#+BEGIN_SRC` and `#+END_SRC`:

```org
#+BEGIN_SRC python
def example():
    pass
#+END_SRC
```

## Lists

```org
- Unordered item
- Another item

1. Ordered item
2. Another item
```

## Blockquotes

```org
#+BEGIN_QUOTE
Quoted text here
#+END_QUOTE
```

## Journal Entry Template

```org
:PROPERTIES:
:JARVIS_ID: YYYYMMDDHHMMSS-topic-slug
:CREATED: YYYY-MM-DDTHH:MM:SSZ
:MODIFIED: YYYY-MM-DDTHH:MM:SSZ
:TYPE: [entry-type]
:SENTIMENT: [sentiment]
:IMPORTANCE: [level]
:AI_SUGGESTED: true
:AI_CONFIDENCE: 0.XX
:AI_GENERATED: false
:END:
#+TAGS: jarvis [type-tag] [context-tag]
#+LINKED_TO: [[Related Note]]

* [Title]

** Original Input

#+BEGIN_QUOTE
[Raw user input preserved exactly]
#+END_QUOTE

** Refined Entry

[Expanded version with context and connections]

*** Context & Connections
- *Related to*: [[Link1]], [[Link2]]
- *Topics*: jarvis, tag1, tag2
- *People*: Names mentioned
- *Systems*: Technical systems involved

-----

*Jarvis Analysis:*
- Confidence: XX%
- Suggested N tags, M links
- Type: [detected type]
```
