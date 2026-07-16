# Jarvis retrieval benchmark

Answers one question: **would model X serve the memory system better than what we run today?**

```bash
make bench                 # fast preset (SciFact + STS-en), ~10 min
make bench PRESET=full     # the verdict run (3 retrieval datasets), ~1h on 4 vCPU
make bench KIND=rerank     # cross-encoders only
```

Results land in `bench/results/<stamp>-<preset>.md` (+ `.json`). **Commit them.** The
scorecard is the record of *why* a model was chosen and the bar the next candidate has
to clear.

## Why public datasets and not the vault

The vault is organic — it grows, changes, and its content is personal. A labelled set
built from it would rot immediately and couldn't be shared or re-run.

It also isn't necessary. The memory system's job is *"find the right passage among
thousands of blobs of text"* — which is **exactly** what a retrieval benchmark with
human relevance judgments measures. BEIR/MTEB datasets give us that, with expert
labels, frozen and reproducible.

## The metrics, and why each exists

| metric | what it's for |
|---|---|
| **nDCG@10** | **The one that decides.** Jarvis has a finite injection budget, so a memory ranked 9th is *invisible* even though it was "found". nDCG rewards putting the right thing **first**; `recall@10` would hide exactly the failure we care about. |
| **PT contamination** | Portuguese must not create **false positives** in English results. Measured as the nDCG@10 *drop* when Portuguese distractors are injected into the English corpus. `0.0000` = correctly ignored. Negative = it's harming real matches. We do **not** measure Portuguese retrieval *quality* — that's a capability we don't need. |
| **θ\*** | The cosine threshold that best separates relevant from irrelevant, calibrated against human labels. Emits the value `memory.context_enrichment.threshold` should take. |
| **STS Spearman** | **Does NOT select models.** Kept only for θ calibration — see the trap below. |
| **ms/query, ms/pair** | Latency on *this* box. A model that can't fit the 2.5 s `UserPromptSubmit` budget is disqualified regardless of quality. |
| **PT tok/word, strips accents** | Tokenizer diagnostics. Accents are *semantic* in Portuguese (`avô` grandfather vs `avó` grandmother); a tokenizer that strips them is lossy, not just inefficient. Also drives the chunk-size budget — the embedder and cross-encoder tokenizers disagree by **−67% … +75%**, so a chunk capped with one can blow the other's 512 ceiling. |

## The trap this harness exists to prevent

We nearly shipped the wrong model twice:

- **`bge-small-en-v1.5`** beat the production model by **+0.080 on STS** and *lost* by
  **−0.042 on nDCG@10**. Picking on STS would have made real retrieval **5.6% worse**.
- A synthetic query set we built by hand gave **opposite verdicts** depending on whether
  queries came from headings or from body sentences.

**Rules that follow:**

1. **Never select on a single dataset.** `full` runs three deliberately diverse retrieval
   sets: SciFact (sparse expert judgments), NFCorpus (dense graded judgments from real user
   queries), ArguAna (**long paragraph queries** — the closest public analogue to "paste a
   blob and ask").
2. **Never select on STS.** It measures symmetric sentence similarity; Jarvis does
   asymmetric query→passage retrieval. They demonstrably disagree.
3. **A reranker must earn its keep.** The only number that justifies one is **Δ nDCG@10 over
   the bi-encoder alone**. A cross-encoder that costs 3 s/query for +0.01 is not worth having.

## Adding a candidate

One line in `registry.py`:

```python
Model("my-new-model", "org/my-new-model", dims=768, note="why we're trying it"),
```

MTEB supplies each model's official query/passage prefixes (E5 needs `query:`/`passage:`,
BGE needs a retrieval instruction). Hand-coding those is the easiest way to rig a
comparison, so we don't.

## Caveats, stated honestly

- **Domain gap.** SciFact is biomedical abstracts; ArguAna is debate arguments. Neither is a
  personal vault. Relative model *ordering* transfers reasonably (that's the premise of
  zero-shot BEIR), but absolute numbers won't match your vault.
- **Production runs INT8 ONNX**, this bench runs fp32 PyTorch. Measured gap on the current
  model: STS-en 0.7711 (fp32) vs 0.7629 (INT8 ONNX). A candidate that only wins by a hair
  here may not win at all after export.
- **Latency is measured on whatever box you run it on.** Run it where it matters.
