"""Jarvis retrieval benchmark CLI.

    uv run --extra bench python -m bench --preset core
    uv run --extra bench python -m bench --preset full --kind embed
    uv run --extra bench python -m bench --preset full --only granite-small-en-r2,multilingual-e5-small

Writes a markdown scorecard + JSON to bench/results/. Commit the scorecard: it is
the record of WHY a model was chosen, and the baseline the next candidate must beat.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .harness import Scorecard, evaluate_embedder, evaluate_reranker, resolve_device
from .registry import EMBEDDERS, PRESETS, RERANKERS

RESULTS = Path(__file__).parent / "results"

# The UserPromptSubmit hook budget. A model that cannot fit this is disqualified
# for the per-prompt path no matter how good its nDCG is.
HOOK_BUDGET_MS = 2500


def _fmt(v, spec="%.4f", dash="—"):
    return dash if v is None else (spec % v if isinstance(v, float) else str(v))


def render(cards: list[Scorecard], preset: str, device: str = "cpu") -> str:
    embed = [c for c in cards if c.kind == "embed"]
    rerank = [c for c in cards if c.kind == "rerank"]
    base = next((c for c in embed if c.is_baseline), None)
    L = [
        "# Jarvis retrieval benchmark",
        "",
        "**Run:** %s · **preset:** `%s` · **device:** `%s`" % (
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), preset, device),
        "",
        ("> Quality metrics (nDCG, STS, theta*) are hardware-independent. "
         "**Latency is not** - this ran on `%s`; production is CPU-only in Docker." % device)
        if device != "cpu" else "",
        "",
        "`nDCG@10` is the headline: Jarvis has a finite injection budget, so a memory "
        "ranked 9th is invisible even though it was found. **STS does not select models** "
        "— it disagrees with retrieval (bge-small: +0.08 STS, −0.04 nDCG). It only "
        "calibrates the threshold.",
        "",
    ]

    if embed:
        L += [
            "## Embedding models",
            "",
            "| model | dim | **nDCG@10** | Δ vs prod | PT contam. | PT in top-10 | STS-en | θ* | ms/query | PT tok/word | strips accents |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for c in sorted(embed, key=lambda x: -(x.ndcg_mean or -1)):
            if c.failed:
                L.append("| `%s` | | **FAILED** — %s |||||||||" % (c.model, c.failed))
                continue
            delta = ("—" if not base or c is base or base.ndcg_mean is None
                     else "%+.4f" % (c.ndcg_mean - base.ndcg_mean))
            sts_en = next((v for k, v in c.sts.items() if k.endswith("/en")), None)
            over = c.ms_per_query and c.ms_per_query > HOOK_BUDGET_MS
            L.append("| %s`%s` | %s | **%s** | %s | %s | %s | %s | %s | %s%s | %s | %s |" % (
                "★ " if c.is_baseline else "", c.model, _fmt(c.dims, "%d"),
                _fmt(c.ndcg_mean), delta,
                _fmt(c.pt_contamination, "%+.4f"), _fmt(c.pt_in_top10, "%.2f"),
                _fmt(sts_en), _fmt(c.theta_star, "%.3f"),
                _fmt(c.ms_per_query, "%.1f"), " ⚠️" if over else "",
                _fmt(c.pt_tokens_per_word, "%.2f"),
                "—" if c.strips_accents is None else ("**YES**" if c.strips_accents else "no"),
            ))
        L += ["", "★ = production baseline. "
                  "**PT contam.** = nDCG@10 change when Portuguese distractors are injected "
                  "into the English corpus; `0.0000` means Portuguese content is correctly "
                  "ignored and does **not** create false positives. Negative = it harms real matches.",
              "**θ\\*** = cosine threshold calibrated against human labels — this is the value "
              "`memory.context_enrichment.threshold` should take (production ships an interim `0.85`; "
              "final calibration still requires labeled real usage).", ""]

    if rerank:
        L += [
            "## Cross-encoders (rerankers)",
            "",
            "A reranker is only worth its cost if **Δ nDCG@10 is positive**. It must also fit "
            "the %d ms hook budget: `ms/pair × candidate_count`." % HOOK_BUDGET_MS,
            "",
            "| model | **nDCG@10** | **Δ over bi-encoder** | ms/pair | 20 cands | 100 cands | strips accents |",
            "|---|---|---|---|---|---|---|",
        ]
        for c in sorted(rerank, key=lambda x: -(x.ndcg_gain or -1)):
            if c.failed:
                L.append("| `%s` | **FAILED** — %s ||||||" % (c.model, c.failed))
                continue
            c20 = c.ms_per_pair * 20 if c.ms_per_pair else None
            c100 = c.ms_per_pair * 100 if c.ms_per_pair else None
            def budget(ms):
                if ms is None:
                    return "—"
                return "%.0f ms%s" % (ms, " ⚠️" if ms > HOOK_BUDGET_MS else " ✅")
            L.append("| %s`%s` | %s | **%s** | %s | %s | %s | %s |" % (
                "★ " if c.is_baseline else "", c.model, _fmt(c.ndcg_mean),
                _fmt(c.ndcg_gain, "%+.4f"), _fmt(c.ms_per_pair, "%.1f"),
                budget(c20), budget(c100),
                "—" if c.strips_accents is None else ("**YES**" if c.strips_accents else "no"),
            ))
        L.append("")

    return "\n".join(L)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bench", description=__doc__)
    p.add_argument("--preset", choices=sorted(PRESETS), default="core")
    p.add_argument("--kind", choices=["embed", "rerank", "both"], default="both")
    p.add_argument("--only", help="comma-separated model names")
    p.add_argument("--top-n", type=int, default=50, help="candidates the reranker rescores")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "mps", "cuda"],
                   help="quality metrics are device-independent; LATENCY IS NOT")
    p.add_argument("--no-latency", action="store_true",
                   help="skip latency (use when benchmarking off the production box)")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    device = resolve_device(a.device)
    measure_latency = not a.no_latency
    tasks = PRESETS[a.preset]
    keep = set(a.only.split(",")) if a.only else None

    embedders = [m for m in EMBEDDERS if not keep or m.name in keep]
    rerankers = [m for m in RERANKERS if not keep or m.name in keep]
    baseline = next(m for m in EMBEDDERS if m.is_baseline)

    cards: list[Scorecard] = []
    if a.kind in ("embed", "both"):
        for m in embedders:
            print("embedding: %s (%s) ..." % (m.name, device), file=sys.stderr, flush=True)
            cards.append(evaluate_embedder(m, tasks, device=device,
                                           measure_latency=measure_latency))
    if a.kind in ("rerank", "both"):
        for m in rerankers:
            print("reranking: %s (%s) ..." % (m.name, device), file=sys.stderr, flush=True)
            cards.append(evaluate_reranker(m, tasks, baseline, top_n=a.top_n, device=device))

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    md = render(cards, a.preset, device)
    (RESULTS / ("%s-%s.md" % (stamp, a.preset))).write_text(md)
    (RESULTS / ("%s-%s.json" % (stamp, a.preset))).write_text(
        json.dumps([c.as_dict() for c in cards], indent=2))
    print(md)
    print("\nwritten to %s" % RESULTS, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
