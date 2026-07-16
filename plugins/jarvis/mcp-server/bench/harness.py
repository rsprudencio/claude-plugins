"""Retrieval benchmark harness for Jarvis.

Answers one question: would model X serve the memory system better than what we
run today? The memory system's job is "find the right passage among thousands of
blobs", which is exactly a retrieval task with human relevance judgments.

Metrics, and why each exists:

  nDCG@10        THE metric. Jarvis has a finite injection budget, so a memory
                 ranked 9th is functionally invisible even though it was found.
                 nDCG rewards putting the right thing FIRST; recall@k hides that.

  PT contamination
                 Portuguese must not create false positives in English results.
                 Measured as the nDCG@10 DROP when PT distractors are injected.

  STS Spearman   NOT a model-selection metric — it disagrees with retrieval (see
                 registry). Used only to CALIBRATE the similarity threshold.

  theta*         The cosine threshold that best separates relevant from
                 irrelevant, derived from human labels. Emits a config value.

  latency        ms/query (embed) and ms/pair (rerank) on THIS box. A model that
                 cannot fit the 2.5s UserPromptSubmit budget is disqualified
                 regardless of quality.

  tokenizer      PT tokens/word, accent preservation, and cross-encoder-vs-
                 embedder token skew. The skew drives the chunk size budget:
                 the two tokenizers disagree by -67%..+75%, so a chunk capped
                 with one can blow the other's 512 ceiling.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from .registry import PT_ACCENT_WORDS, Model, TaskSet

logger = logging.getLogger("jarvis-bench")

_LATENCY_PROBE = "what did we decide about the reranker latency budget"


def resolve_device(requested: str = "auto") -> str:
    """Pick a torch device.

    QUALITY metrics (nDCG, STS, theta*) are hardware-independent — run them
    wherever is fastest (an M-series GPU via MPS is ~10x a CPU-only container).

    LATENCY is NOT. Production runs CPU-only inside Docker on 4 vCPU, so ms/query
    measured on a Mac GPU is meaningless for the 2.5s hook budget. Measure latency
    where it actually runs (`--latency-only` in the container), and never quote a
    number without its device.
    """
    import torch
    if requested != "auto":
        return requested
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


@dataclass
class Scorecard:
    model: str
    hf_path: str
    kind: str
    dims: int | None = None
    is_baseline: bool = False
    note: str = ""

    ndcg: dict[str, float] = field(default_factory=dict)     # task -> nDCG@10
    ndcg_mean: float | None = None

    pt_clean: float | None = None        # nDCG@10, English corpus only
    pt_polluted: float | None = None     # nDCG@10, + Portuguese distractors
    pt_contamination: float | None = None  # polluted - clean  (negative = harm)
    pt_in_top10: float | None = None     # avg # of PT distractors in top-10

    sts: dict[str, float] = field(default_factory=dict)      # "task/lang" -> rho
    theta_star: float | None = None      # recommended cosine threshold
    theta_accuracy: float | None = None

    ms_per_query: float | None = None    # embedders
    ms_per_pair: float | None = None     # rerankers
    device: str = "cpu"                  # quality is device-independent; LATENCY IS NOT
    ndcg_gain: float | None = None       # rerankers: delta over the bi-encoder

    pt_tokens_per_word: float | None = None
    strips_accents: bool | None = None

    failed: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── evaluation ────────────────────────────────────────────────────────────

def _ndcg_at_10(scores: np.ndarray, doc_ids: list[str], qids: list[str],
                qrels: dict[str, set[str]]) -> float:
    """scores: (n_docs, n_queries)."""
    total = 0.0
    for j, q in enumerate(qids):
        top = np.argsort(-scores[:, j])[:10]
        gains = [1.0 if doc_ids[i] in qrels.get(q, ()) else 0.0 for i in top]
        dcg = sum(g / np.log2(i + 2) for i, g in enumerate(gains))
        ideal = sum(1 / np.log2(i + 2) for i in range(min(10, len(qrels.get(q, ())))))
        total += dcg / ideal if ideal else 0.0
    return total / len(qids) if qids else 0.0


def _tokenizer_diagnostics(card: Scorecard, hf_path: str) -> None:
    """Portuguese tokenization: efficiency and lossiness.

    Accents are semantic in Portuguese (avo/avô/avó are three different words),
    so a tokenizer that strips them is destroying meaning, not just fragmenting.
    """
    try:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(hf_path)
        n_tokens = sum(len(tok.tokenize(w)) for w in PT_ACCENT_WORDS)
        card.pt_tokens_per_word = round(n_tokens / len(PT_ACCENT_WORDS), 2)
        lossy = False
        for w in PT_ACCENT_WORDS:
            rt = tok.convert_tokens_to_string(tok.tokenize(w)).replace(" ", "").replace("##", "")
            if rt.lower() != w.lower():
                lossy = True
                break
        card.strips_accents = lossy
    except Exception as e:  # pragma: no cover - diagnostic only
        logger.warning("tokenizer diagnostics failed for %s: %s", hf_path, e)


def _calibrate_theta(cos: np.ndarray, gold: np.ndarray) -> tuple[float, float]:
    """Find the cosine threshold best separating relevant (>=3) from not (<=1).

    This is how the injection threshold should be set. The current production
    value (0.5) sits far below the operating range: on STS-B, the WORST possible
    pair still scores ~0.80 cosine, so theta=0.5 admits 100% of irrelevant
    content. It is a filter that filters nothing.
    """
    pos, neg = cos[gold >= 3], cos[gold <= 1]
    if not len(pos) or not len(neg):
        return float("nan"), float("nan")
    # Round the GRID, not the winner. np.arange yields 0.6000000000000001, and
    # rounding that to 0.600 afterwards hands back a threshold that scores worse
    # than the one actually selected.
    best_acc, best_t = 0.0, 0.5
    for t in np.round(np.arange(0.30, 0.999, 0.005), 3):
        acc = ((pos >= t).sum() + (neg < t).sum()) / (len(pos) + len(neg))
        if acc > best_acc:
            best_acc, best_t = acc, float(t)
    return best_t, round(float(best_acc), 4)


def evaluate_embedder(m: Model, tasks: TaskSet, encode_bs: int = 64,
                      device: str = "cpu", measure_latency: bool = True) -> Scorecard:
    import mteb
    from sentence_transformers import SentenceTransformer
    from scipy.stats import spearmanr

    card = Scorecard(model=m.name, hf_path=m.hf_path, kind="embed", dims=m.dims,
                     is_baseline=m.is_baseline, note=m.note)
    try:
        card.device = device
        model = SentenceTransformer(m.hf_path, device=device,
                                    trust_remote_code=m.trust_remote_code)
        model.max_seq_length = 512
        card.dims = model.get_sentence_embedding_dimension()
        _tokenizer_diagnostics(card, m.hf_path)

        def enc(texts: list[str]) -> np.ndarray:
            return model.encode(texts, batch_size=encode_bs, normalize_embeddings=True,
                                show_progress_bar=False)

        # ── retrieval (the metric that matters) ──
        # Keep the FIRST task's encodings: the PT-contamination test reuses them.
        # Re-encoding a 5k-doc corpus is ~15% of a model's runtime, for nothing.
        cached: dict[str, tuple] = {}
        for name in tasks.retrieval_en:
            corpus, queries, qrels = _load_retrieval(name)
            doc_ids = list(corpus)
            qids = list(qrels)
            D = enc([corpus[d] for d in doc_ids])
            Q = enc([queries[q] for q in qids])
            card.ndcg[name] = round(float(_ndcg_at_10(D @ Q.T, doc_ids, qids, qrels)), 4)
            if name == tasks.retrieval_en[0]:
                cached[name] = (doc_ids, qids, qrels, D, Q)
        if card.ndcg:
            card.ndcg_mean = round(statistics.mean(card.ndcg.values()), 4)

        # ── Portuguese contamination: does PT content harm English results? ──
        if tasks.pt_distractor_task and cached:
            base = tasks.retrieval_en[0]
            doc_ids, qids, qrels, D, Q = cached[base]
            pt_docs = _load_pt_distractors(tasks.pt_distractor_task,
                                           tasks.pt_distractor_docs)
            card.pt_clean = card.ndcg[base]   # identical computation — reuse it

            P = enc(pt_docs)
            D2 = np.vstack([D, P])
            ids2 = doc_ids + ["__PT__%d" % i for i in range(len(pt_docs))]
            S2 = D2 @ Q.T
            card.pt_polluted = round(float(_ndcg_at_10(S2, ids2, qids, qrels)), 4)
            card.pt_contamination = round(card.pt_polluted - card.pt_clean, 4)
            intruders = [
                sum(1 for i in np.argsort(-S2[:, j])[:10] if ids2[i].startswith("__PT__"))
                for j in range(len(qids))
            ]
            card.pt_in_top10 = round(statistics.mean(intruders), 2)

        # ── STS: threshold calibration only, NOT model selection ──
        for task_name, lang in tasks.sts:
            a, b, gold = _load_sts(task_name, lang)
            if not len(gold):
                continue
            A, B = enc(a), enc(b)
            cos = (A * B).sum(1)
            card.sts["%s/%s" % (task_name, lang)] = round(
                float(spearmanr(cos, gold).correlation), 4)
            if lang == "en" and card.theta_star is None:
                card.theta_star, card.theta_accuracy = _calibrate_theta(cos, gold)

        # ── latency: ONLY meaningful on the hardware production runs on ──
        if measure_latency:
            model.encode([_LATENCY_PROBE], show_progress_bar=False)  # warm
            t0 = time.perf_counter()
            for _ in range(20):
                model.encode([_LATENCY_PROBE], show_progress_bar=False)
            card.ms_per_query = round((time.perf_counter() - t0) / 20 * 1000, 1)

    except Exception as e:
        logger.exception("embedder %s failed", m.name)
        card.failed = str(e)[:120]
    return card


def evaluate_reranker(m: Model, tasks: TaskSet, baseline_embedder: Model,
                      top_n: int = 50, device: str = "cpu") -> Scorecard:
    """Two-stage, exactly as production does it: bi-encoder recalls, CE rescores.

    The only number that justifies a reranker is nDCG GAIN over the bi-encoder
    alone. A reranker that costs 3s/query and adds +0.01 is not worth having in a
    2.5s hook.
    """
    from sentence_transformers import CrossEncoder, SentenceTransformer

    card = Scorecard(model=m.name, hf_path=m.hf_path, kind="rerank",
                     is_baseline=m.is_baseline, note=m.note)
    try:
        card.device = device
        _tokenizer_diagnostics(card, m.hf_path)
        be = SentenceTransformer(baseline_embedder.hf_path, device=device,
                                 trust_remote_code=baseline_embedder.trust_remote_code)
        be.max_seq_length = 512
        ce = CrossEncoder(m.hf_path, device=device, max_length=512,
                          trust_remote_code=m.trust_remote_code)

        gains, pair_ms = [], []
        for name in tasks.retrieval_en:
            corpus, queries, qrels = _load_retrieval(name)
            doc_ids, qids = list(corpus), list(qrels)
            D = be.encode([corpus[d] for d in doc_ids], batch_size=64,
                          normalize_embeddings=True, show_progress_bar=False)
            Q = be.encode([queries[q] for q in qids], batch_size=64,
                          normalize_embeddings=True, show_progress_bar=False)
            S = D @ Q.T
            cands = [list(np.argsort(-S[:, j])[:top_n]) for j in range(len(qids))]
            before = _ndcg_at_10(S, doc_ids, qids, qrels)

            reranked = np.full_like(S, -1e9)
            t0, n_pairs = time.perf_counter(), 0
            for j, q in enumerate(qids):
                pairs = [[queries[q], corpus[doc_ids[i]]] for i in cands[j]]
                sc = ce.predict(pairs, batch_size=64, show_progress_bar=False)
                n_pairs += len(pairs)
                for i, s in zip(cands[j], np.asarray(sc)):
                    reranked[i, j] = float(s)
            pair_ms.append((time.perf_counter() - t0) / max(n_pairs, 1) * 1000)

            after = _ndcg_at_10(reranked, doc_ids, qids, qrels)
            card.ndcg[name] = round(float(after), 4)
            gains.append(after - before)

        if card.ndcg:
            card.ndcg_mean = round(statistics.mean(card.ndcg.values()), 4)
        if gains:
            card.ndcg_gain = round(statistics.mean(gains), 4)
        if pair_ms:
            card.ms_per_pair = round(statistics.mean(pair_ms), 1)

    except Exception as e:
        logger.exception("reranker %s failed", m.name)
        card.failed = str(e)[:120]
    return card


# ── dataset loading (via MTEB, so we don't hand-roll qrels) ────────────────

def _pick_split(task, available) -> str:
    """Prefer the TEST split.

    Do NOT use eval_splits[0]: some tasks declare ['dev', 'test'] (STS-B does),
    so index 0 silently evaluates on dev. That produced a 0.77 -> 0.83 phantom
    improvement before it was caught. Always evaluate on held-out test data.
    """
    declared = list(task.metadata.eval_splits or [])
    for candidate in ("test", *reversed(declared)):
        if candidate in available:
            return candidate
    return next(iter(available))


def _retrieval_split(task_name: str, subset: str | None = None):
    """MTEB 2.x nests as dataset[subset][split] -> {corpus, queries, relevant_docs}."""
    import mteb
    task = mteb.get_tasks(tasks=[task_name])[0]
    task.load_data()
    ds = task.dataset
    key = subset if subset in ds else next(
        (k for k in ds if subset and k.startswith(subset)), next(iter(ds)))
    per_split = ds[key]
    return per_split[_pick_split(task, per_split)]


def _doc_text(row: dict) -> str:
    return ((row.get("title") or "") + " " + (row.get("text") or "")).strip()


def _load_retrieval(task_name: str) -> tuple[dict[str, str], dict[str, str], dict[str, set[str]]]:
    d = _retrieval_split(task_name)
    corpus = {r["id"]: _doc_text(r) for r in d["corpus"]}
    queries = {r["id"]: r["text"] for r in d["queries"]}
    qrels = {
        q: {doc for doc, s in rel.items() if int(s) > 0}
        for q, rel in d["relevant_docs"].items()
    }
    qrels = {q: v for q, v in qrels.items() if v and q in queries}
    return corpus, queries, qrels


def _load_pt_distractors(task_name: str, limit: int) -> list[str]:
    """Portuguese documents from an UNRELATED corpus — relevant to no query.

    These are pure noise: they are in no query's qrels, so any that surface in
    the top-10 are false positives by construction. A model scores well here by
    IGNORING Portuguese, not by understanding it.
    """
    d = _retrieval_split(task_name, subset="por")
    out = []
    for row in d["corpus"]:
        t = _doc_text(row)
        if t:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _load_sts(task_name: str, lang: str) -> tuple[list[str], list[str], np.ndarray]:
    import mteb
    task = mteb.get_tasks(tasks=[task_name])[0]
    task.load_data()
    ds = task.dataset
    if lang in ds:
        ds = ds[lang]
    elif any(k.startswith(lang) for k in ds):
        ds = ds[next(k for k in ds if k.startswith(lang))]
    ds = ds[_pick_split(task, ds)]
    a = list(ds["sentence1"])
    b = list(ds["sentence2"])
    gold = np.asarray(ds["score"], dtype=float)
    return a, b, gold
