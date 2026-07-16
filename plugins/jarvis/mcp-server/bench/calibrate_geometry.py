"""ArguAna-guided calibration of embedding-geometry transforms for asymmetric retrieval.

Question: can a post-hoc geometry transform of granite-small-en-r2's embeddings improve
the *comparison* (ranking) on our asymmetric prompt->passage task? Judged by nDCG@10.

Compass  : ArguAna   (long-paragraph query -> counter-passage; closest public analogue to
                       "paste a blob, find the relevant memory"). Higher = better.
Guardrails: SciFact, NFCorpus (short queries) — so a transform that wins ArguAna but craters
            short-query retrieval is visible (the handover's "never pick on one dataset" rule).

Strategies (all torch-free numpy ops, fit on the DOCUMENT/corpus distribution, applied
identically to query and passage embeddings, then re-L2-normalized):
  baseline    : plain cosine (should reproduce the handover's ArguAna ~0.3964 as a sanity check)
  mean_center : subtract corpus mean
  abtt_kN     : all-but-the-top — remove mean + top-N principal directions (anisotropy fix)
  whiten      : ZCA whitening on corpus covariance

Run:
  uv run --directory plugins/jarvis/mcp-server --extra bench python -m bench.calibrate_geometry
"""
from __future__ import annotations

import numpy as np

from .harness import _load_retrieval, _ndcg_at_10, resolve_device

MODEL = "ibm-granite/granite-embedding-small-english-r2"
DATASETS = ["ArguAna", "SciFact", "NFCorpus"]
ABTT_KS = [1, 3, 5, 10]


def l2norm(X: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(X, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return X / n


def fit_corpus_stats(D: np.ndarray):
    """Fit mean + covariance eigendecomposition on the document (corpus) distribution."""
    mu = D.mean(axis=0, keepdims=True)          # (1, dim)
    Dc = D - mu
    cov = (Dc.T @ Dc) / Dc.shape[0]             # (dim, dim), cheap at dim=384
    evals, evecs = np.linalg.eigh(cov)          # ascending, evecs columns
    order = np.argsort(evals)[::-1]
    return mu, np.clip(evals[order], 0.0, None), evecs[:, order]


def t_mean_center(X, mu, *_):
    return l2norm(X - mu)


def t_abtt(X, mu, evals, evecs, k):
    Xc = X - mu
    if k > 0:
        U = evecs[:, :k]                        # (dim, k) top-k principal dirs
        Xc = Xc - (Xc @ U) @ U.T
    return l2norm(Xc)


def t_whiten(X, mu, evals, evecs, eps=1e-6):
    W = evecs @ np.diag(1.0 / np.sqrt(evals + eps)) @ evecs.T   # ZCA
    return l2norm((X - mu) @ W)


def run():
    device = resolve_device("auto")
    print(f"# device={device}  model={MODEL}\n")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL, device=device, trust_remote_code=True)
    model.max_seq_length = 512

    def enc(texts):
        v = model.encode(texts, batch_size=64, normalize_embeddings=True,
                          show_progress_bar=False)
        return np.asarray(v, dtype=np.float64)

    table = {}
    for ds in DATASETS:
        corpus, queries, qrels = _load_retrieval(ds)
        doc_ids, qids = list(corpus), list(qrels)
        print(f"[{ds}] docs={len(doc_ids)} queries={len(qids)} — encoding...")
        D = enc([corpus[d] for d in doc_ids])
        Q = enc([queries[q] for q in qids])
        mu, evals, evecs = fit_corpus_stats(D)

        strategies = {"baseline": lambda X: l2norm(X),
                      "mean_center": lambda X: t_mean_center(X, mu)}
        for k in ABTT_KS:
            strategies[f"abtt_k{k}"] = (lambda X, k=k: t_abtt(X, mu, evals, evecs, k))
        strategies["whiten"] = lambda X: t_whiten(X, mu, evals, evecs)

        table[ds] = {}
        for name, fn in strategies.items():
            Dp, Qp = fn(D), fn(Q)
            ndcg = float(_ndcg_at_10(Dp @ Qp.T, doc_ids, qids, qrels))
            table[ds][name] = round(ndcg, 4)
            print(f"    {name:12s} nDCG@10 = {ndcg:.4f}")
        print()

    # ---- summary table (compass = ArguAna), deltas vs baseline ----
    names = list(next(iter(table.values())).keys())
    print("=" * 68)
    print(f"{'strategy':14s}" + "".join(f"{ds:>12s}" for ds in DATASETS)
          + f"{'ArguAna Δ':>12s}")
    print("-" * 68)
    base_arg = table["ArguAna"]["baseline"]
    for n in names:
        row = f"{n:14s}" + "".join(f"{table[ds][n]:>12.4f}" for ds in DATASETS)
        d = table["ArguAna"][n] - base_arg
        star = "  <-- best" if table["ArguAna"][n] == max(
            table["ArguAna"].values()) and n != "baseline" else ""
        print(row + f"{d:>+12.4f}" + star)
    print("=" * 68)
    print("Compass = ArguAna nDCG@10. SciFact/NFCorpus are short-query guardrails:")
    print("a strategy that lifts ArguAna but drops those is a red flag, not a win.")


if __name__ == "__main__":
    run()
