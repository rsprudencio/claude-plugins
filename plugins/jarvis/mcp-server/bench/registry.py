"""Model and task registry for the Jarvis retrieval benchmark.

Adding a new candidate model is a one-line change here. Everything else
(scoring, latency, tokenizer diagnostics, scorecard) is derived.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Model:
    """A candidate model.

    prompt_name is MTEB's own prompt registry key. When set, MTEB applies the
    model's official query/passage prefixes (e5 needs "query:"/"passage:", bge
    needs a retrieval instruction). Hand-coding these is the single easiest way
    to rig a comparison, so we let MTEB do it.
    """

    name: str
    hf_path: str
    kind: str = "embed"          # "embed" | "rerank"
    dims: int | None = None
    note: str = ""
    is_baseline: bool = False    # what production runs today
    trust_remote_code: bool = False
    """Executes arbitrary Python from the model repo. OFF by default.

    Some architectures (e.g. gte-multilingual) ship custom modelling code and
    will not load without it. That is a supply-chain decision, so it is made
    per model and in the open — never blanket-enabled for the whole registry.
    """


# ── Embedding candidates ──────────────────────────────────────────────────

EMBEDDERS: list[Model] = [
    Model("granite-small-en-r2", "ibm-granite/granite-embedding-small-english-r2",
          dims=384, is_baseline=True, note="production baseline (R2, English-only)"),

    # R2-generation multilingual — same lineage as the baseline. NOT to be confused
    # with granite-107m-multilingual, which is R1 and scores far worse (0.6325).
    Model("granite-97m-multi-r2", "ibm-granite/granite-embedding-97m-multilingual-r2",
          dims=384, note="R2 multilingual, small"),
    Model("granite-311m-multi-r2", "ibm-granite/granite-embedding-311m-multilingual-r2",
          dims=768, note="R2 multilingual, large; ships ONNX incl. quantized"),

    Model("granite-107m-multi-r1", "ibm-granite/granite-embedding-107m-multilingual",
          dims=384, note="R1 generation — kept as a control"),

    Model("bge-small-en-v1.5", "BAAI/bge-small-en-v1.5", dims=384,
          note="BERT-uncased: STRIPS ACCENTS (graça -> graca)"),
    Model("bge-base-en-v1.5", "BAAI/bge-base-en-v1.5", dims=768),

    # Multi-functional: one model does dense (measured here), sparse, AND
    # ColBERT multi-vector rerank. Only the DENSE mode is measurable via this
    # harness — the late-interaction/MaxSim rerank path needs custom scoring
    # code (SentenceTransformer/CrossEncoder can't express it). 1024-dim ->
    # would force a schema migration off the 384 baseline. See bge-reranker-v2-m3
    # for the m3-family cross-encoder that IS measurable in the rerank track.
    Model("bge-m3", "BAAI/bge-m3", dims=1024,
          note="multilingual (100+ langs), 8192 ctx; DENSE mode only here"),
    Model("e5-small-v2", "intfloat/e5-small-v2", dims=384),
    Model("multilingual-e5-small", "intfloat/multilingual-e5-small", dims=384),
    Model("multilingual-e5-base", "intfloat/multilingual-e5-base", dims=768),
]

# ── Cross-encoder (reranker) candidates ───────────────────────────────────

RERANKERS: list[Model] = [
    Model("ms-marco-MiniLM-L-6-v2", "cross-encoder/ms-marco-MiniLM-L-6-v2",
          kind="rerank", is_baseline=True,
          note="production baseline; BERT-uncased, STRIPS ACCENTS"),
    Model("ms-marco-MiniLM-L-12-v2", "cross-encoder/ms-marco-MiniLM-L-12-v2",
          kind="rerank"),
    Model("mmarco-mMiniLMv2-L12", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
          kind="rerank", note="multilingual MS MARCO"),
    Model("bge-reranker-base", "BAAI/bge-reranker-base", kind="rerank",
          note="multilingual, ~4x larger — check latency"),
    Model("mxbai-rerank-xsmall-v1", "mixedbread-ai/mxbai-rerank-xsmall-v1",
          kind="rerank"),

    # The m3-family cross-encoder — reranking counterpart to bge-m3. This IS a
    # standard CrossEncoder (single relevance logit), so the harness measures it
    # natively, unlike bge-m3's own ColBERT rerank mode. Use this to gauge
    # m3-family reranking quality without writing custom MaxSim scoring.
    Model("bge-reranker-v2-m3", "BAAI/bge-reranker-v2-m3", kind="rerank",
          note="multilingual m3-family CE; pairs with bge-m3"),
]


# ── Tasks ─────────────────────────────────────────────────────────────────
#
# The memory system's job is "find the right passage among thousands of blobs",
# which is exactly what a retrieval task with qrels measures. nDCG@10 is the
# headline metric because Jarvis has a finite injection budget: a memory ranked
# 9th is functionally invisible even though it was "found".
#
# STS tasks do NOT produce nDCG. They are here only to calibrate the similarity
# threshold (theta) against human similarity judgments.

@dataclass(frozen=True)
class TaskSet:
    retrieval_en: list[str] = field(default_factory=list)
    sts: list[tuple[str, str]] = field(default_factory=list)   # (task, lang)
    pt_distractor_task: str | None = None
    pt_distractor_docs: int = 2000


# ── The Portuguese requirement, stated precisely ──────────────────────────
#
# The vault is ~91% English and the memories are English. Portuguese retrieval
# QUALITY is not a requirement. The requirement is that Portuguese content must
# not POLLUTE English results with false positives.
#
# So we do NOT measure "nDCG on Portuguese queries" (wrong metric — it rewards a
# capability we don't need). We measure CONTAMINATION:
#
#   1. nDCG@10 on English queries, English corpus            -> clean baseline
#   2. nDCG@10 on English queries, corpus + PT distractors   -> polluted
#   3. delta = (2) - (1)                                     -> THE HARM
#
# The distractors are Portuguese documents from an UNRELATED corpus, so they are
# in no query's qrels. Any that surface in the top-10 are pure false positives.
# A model scores well here by IGNORING Portuguese, not by understanding it.
# (Measured on the live vault today: PT chunks are 4-7x UNDER-represented in
# English top-10 vs their 9.2% base rate — i.e. no pollution. This test guards
# that property against a model swap.)

# Fast preset — for iterating.
CORE = TaskSet(
    retrieval_en=["SciFact"],
    sts=[("STSBenchmarkMultilingualSTS", "en")],
    pt_distractor_task="MultilingualNanoArguAnaRetrieval",
)

# Full preset — the verdict run. Three retrieval datasets, deliberately diverse:
#   SciFact  — expert-annotated scientific claims; sparse judgments (1.1/query)
#   NFCorpus — real user queries, DENSE graded judgments (~38/query)
#   ArguAna  — LONG paragraph queries; mirrors the "paste a blob, ask" pattern
# A model that wins on one dataset is a coin flip. Winning on all three is signal.
# (We learned this the hard way: bge-small beat granite by +0.08 on STS and LOST
# by -0.04 on SciFact. Picking on a single benchmark hands you the wrong model.)
FULL = TaskSet(
    retrieval_en=["SciFact", "NFCorpus", "ArguAna"],
    sts=[
        ("STSBenchmarkMultilingualSTS", "en"),
        ("STSBenchmarkMultilingualSTS", "pt"),   # informational only
        ("Assin2STS", "pt"),                     # native Brazilian PT, not MT
    ],
    pt_distractor_task="MultilingualNanoArguAnaRetrieval",
)

# ArguAna-only — fast single-dataset probe. ArguAna is the long-paragraph-query
# asymmetric dataset, the closest public analogue to prompt->memory matching, so it
# is our compass for reranker gain without paying for SciFact/NFCorpus each run.
ARG = TaskSet(retrieval_en=["ArguAna"])

PRESETS = {"core": CORE, "full": FULL, "arg": ARG}

# Accented Portuguese words used for the tokenizer diagnostic. Accents are
# semantic in Portuguese (avô=grandfather vs avó=grandmother), so a tokenizer
# that strips them is lossy, not merely inefficient.
PT_ACCENT_WORDS = [
    "graça", "coração", "não", "ação", "você", "três", "mãe", "açúcar", "avô", "avó",
]
