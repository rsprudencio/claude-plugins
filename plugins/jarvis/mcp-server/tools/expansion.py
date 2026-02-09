"""Rule-based query expansion for semantic search.

Expands user queries with synonym and intent terms to improve recall
when the query vocabulary doesn't match the document vocabulary.

MVP: rule-based only (dictionary synonyms + intent patterns).
Haiku-based expansion deferred to future release.
"""
import re
from typing import Dict, List, Optional, Tuple


# Default synonym mappings: trigger word -> expansion terms
DEFAULT_SYNONYMS: Dict[str, List[str]] = {
    "auth": ["authentication", "authorization", "login", "oauth"],
    "db": ["database", "postgres", "sqlite", "sql"],
    "k8s": ["kubernetes", "cluster", "pods", "deployment"],
    "config": ["configuration", "settings", "environment", "env"],
    "api": ["endpoint", "rest", "http", "request"],
    "deploy": ["deployment", "release", "ci/cd", "pipeline"],
    "test": ["testing", "pytest", "unittest", "spec"],
    "bug": ["error", "issue", "defect", "fix"],
    "perf": ["performance", "optimization", "latency", "speed"],
    "ui": ["interface", "frontend", "component", "react"],
    "infra": ["infrastructure", "terraform", "cloud", "aws"],
    "doc": ["documentation", "readme", "guide", "docs"],
    "sec": ["security", "vulnerability", "cve", "audit"],
    "monitor": ["monitoring", "observability", "metrics", "alerting"],
    "log": ["logging", "logs", "trace", "debug"],
}

# Intent patterns: regex -> (intent_name, expansion_terms)
DEFAULT_INTENT_PATTERNS: List[Tuple[str, str, List[str]]] = [
    (r"\bhow\s+(?:to|do|can)\b", "how-to", ["guide", "steps", "tutorial"]),
    (r"\bwhy\s+(?:did|do|was|is)\b", "rationale", ["reason", "decision", "rationale"]),
    (r"\bwhen\s+(?:did|was|will)\b", "timeline", ["date", "timeline", "history"]),
    (r"\bshould\s+(?:I|we)\b", "decision", ["decision", "tradeoff", "recommendation"]),
]

_DEFAULT_MAX_TERMS = 5


def expand_query(query: str, config: Optional[dict] = None) -> dict:
    """Expand a search query with synonym and intent terms.

    Args:
        query: Original user query
        config: Optional overrides for enabled, max_expansion_terms, synonyms, intent_patterns

    Returns:
        Dict with:
          - original: the original query
          - expanded: the expanded query string (or original if disabled/no expansion)
          - terms_added: list of added terms
          - intent: detected intent name or None
          - enabled: whether expansion was active
    """
    config = config or {}
    enabled = config.get("enabled", True)
    max_terms = config.get("max_expansion_terms", _DEFAULT_MAX_TERMS)
    synonyms = {**DEFAULT_SYNONYMS, **config.get("synonyms", {})}
    intent_patterns = config.get("intent_patterns", DEFAULT_INTENT_PATTERNS)

    if not enabled:
        return {
            "original": query,
            "expanded": query,
            "terms_added": [],
            "intent": None,
            "enabled": False,
        }

    terms, intent = _extract_expansion_terms(query, synonyms, intent_patterns)
    unique_terms = _deduplicate_terms(query, terms)

    # Cap expansion terms
    unique_terms = unique_terms[:max_terms]

    if unique_terms:
        expanded = query + " " + " ".join(unique_terms)
    else:
        expanded = query

    return {
        "original": query,
        "expanded": expanded,
        "terms_added": unique_terms,
        "intent": intent,
        "enabled": True,
    }


def _extract_expansion_terms(
    query: str,
    synonyms: Dict[str, List[str]],
    intent_patterns: list,
) -> Tuple[List[str], Optional[str]]:
    """Extract expansion terms from synonym matches and intent patterns.

    Returns (terms, intent_name).
    """
    terms = []
    query_lower = query.lower()

    # Synonym expansion
    for trigger, expansions in synonyms.items():
        # Match trigger as a whole word
        if re.search(rf'\b{re.escape(trigger)}\b', query_lower):
            terms.extend(expansions)

    # Intent detection
    intent = None
    for pattern, intent_name, intent_terms in intent_patterns:
        if re.search(pattern, query_lower):
            intent = intent_name
            terms.extend(intent_terms)
            break  # Use first matching intent

    return terms, intent


def _deduplicate_terms(query: str, expansion_terms: List[str]) -> List[str]:
    """Remove expansion terms that already appear in the query.

    Case-insensitive deduplication.
    """
    query_words = set(query.lower().split())
    seen = set()
    unique = []
    for term in expansion_terms:
        term_lower = term.lower()
        if term_lower not in query_words and term_lower not in seen:
            seen.add(term_lower)
            unique.append(term)
    return unique
