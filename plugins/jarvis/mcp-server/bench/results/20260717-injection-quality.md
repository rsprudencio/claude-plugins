# Jarvis injection quality calibration

**Run:** 2026-07-17 · **live corpus:** 3,043 documents · **cases:** 38
(19 positive, 19 hard negative)

**Selected raw-cosine threshold: `0.876`**

The quality constraint is at least 95% hard-negative rejection. Because this dataset
has 19 negatives, that requires 19/19 rejected. Within that constraint, the selector
maximizes positive recall and chooses the lower threshold when quality and recall tie.

| threshold | case precision | positive recall | negative rejection | mean matches |
|---:|---:|---:|---:|---:|
| 0.780 | 0.529 | 0.947 | 0.158 | 15.29 |
| 0.800 | 0.545 | 0.947 | 0.210 | 11.29 |
| 0.820 | 0.556 | 0.789 | 0.368 | 4.92 |
| 0.840 | 0.722 | 0.684 | 0.737 | 1.92 |
| 0.850 | 0.786 | 0.579 | 0.842 | 1.18 |
| 0.855 | 0.800 | 0.421 | 0.895 | 0.82 |
| 0.860 | 0.875 | 0.368 | 0.947 | 0.68 |
| 0.864 | 0.875 | 0.368 | 0.947 | 0.63 |
| 0.868 | 0.857 | 0.316 | 0.947 | 0.40 |
| 0.872 | 0.833 | 0.263 | 0.947 | 0.34 |
| **0.876** | **1.000** | **0.263** | **1.000** | **0.29** |
| 0.878 | 1.000 | 0.263 | 1.000 | 0.26 |
| 0.880 | 1.000 | 0.210 | 1.000 | 0.18 |

This is intentionally a precision-first deployment gate: it abstains on 14 of 19
positive paraphrases. Improving that recall without restoring false injections requires
a better comparison-quality stage (the planned reranker), not a looser cosine cutoff.
