# Host inference proof and production cutover — 2026-07-21

This records both the isolated local proof and the completed production
cutover. Only model inference moved to the macOS host; Jarvis and PostgreSQL
remain containerized.

## Environment

- macOS arm64 on Apple M5 Max
- `llama.cpp` Homebrew build 10050 (`b15ca938a`)
- Metal device discovered as `MTL0: Apple M5 Max`
- Both processes loaded `libggml-metal.so`
- Granite process RSS after warmup: approximately 168 MiB
- BGE process RSS after warmup: approximately 801 MiB

## Functional results

| Check | Result |
| --- | --- |
| Granite health | pass |
| BGE health | pass |
| Granite dimensions | 384 |
| Granite L2 norm | 1.00000005 |
| Granite first-request latency | 71.5 ms |
| BGE semantic top result | correct |
| BGE three-document first-request latency | 168.3 ms |

Warm smoke observations after LaunchAgent installation were 19.6 ms for one
embedding and 58.0 ms for three-document reranking.

The latency samples above are smoke-test observations, not statistically
useful benchmarks.

## Pre-cutover ONNX comparison

Four representative texts were encoded through both the running Jarvis ONNX
backend and the isolated llama.cpp Granite F16 endpoint. Per-text cosine
similarity between the output vectors was:

```text
0.99400657
0.99373408
0.99051729
0.99322593
```

Mean similarity was `0.99287097`; minimum similarity was `0.99051729`.
The production cutover therefore used the safer clean-reindex path instead of
assuming the two implementations were exactly interchangeable.

## Colima/container reachability

The existing healthy `jarvis-jarvis-1` container reached both services using
`host.docker.internal`:

- `GET :8751/health` returned `{"status":"ok"}`.
- `GET :8752/health` returned `{"status":"ok"}`.
- A real `/v1/embeddings` request returned HTTP 200.
- A real `/v1/rerank` request ranked the pgvector memory passage above the
  distractor.

No Docker configuration, Jarvis runtime configuration, PostgreSQL data, or
stored embeddings were changed during this proof.

## Production cutover

- Installed persistent user LaunchAgents on ports 8751 and 8752, bound to
  loopback and configured for Metal.
- Activated host Granite embeddings and host BGE reranking through
  `host.docker.internal`; created a full pre-cutover config backup at
  `~/.jarvis/config.json.pre-host-inference.bak`.
- Atomically re-embedded 196 existing memories in 0.832 seconds. The
  pre/post `updated_at` fingerprint remained
  `ac92453983d2417a0064308504781147`.
- Force-indexed 304 vault files into 3,277 chunks in 22.88 seconds with zero
  errors. Twenty-six files followed normal skip rules, including three files
  rejected by secret detection.
- A production cross-store query completed in 478.6 ms with BGE applied to 31
  deduplicated candidates. A vault-only query completed in 249.6 ms and ranked
  the two passage-ranking roadmaps first.
- Before its removal, the isolated ONNX implementation returned a normalized
  384d vector and passed the canonical Granite model-consistency check without
  changing the live host-backed server.

The live store later contained 200 memories (154 active) plus 3,277 vault
chunks across 304 files; the additional memories were normal writes after the
196-row migration snapshot.

## Host-only image

After the host path proved stable, the Docker image removed the Granite and
MiniLM ONNX weights plus PyTorch, SentenceTransformers, tokenizers, and
ONNX Runtime. The image and Compose defaults now select the host Granite and
BGE endpoints. The running-container verification checks that `/app/models`
is absent and those local runtime modules are not installed before exercising
live embedding, reranking, and context-enrichment requests.

The final production image (`sha256:97a3fecab4f8`) is 252,333,361 bytes,
down from 638,200,720 bytes for the rollback-capable image. In the running
container, all four local inference module probes returned absent,
`/app/models` did not exist, and the active environment reported host
embedding plus enabled host reranking. A live request returned a normalized
384d embedding; BGE reversed a deliberately incorrect two-document vector
ranking; and context enrichment returned three matches from 3,440 indexed
records. Both native LaunchAgents and all Jarvis HTTP services, including
Todoist, were healthy with zero container restarts.

The obsolete `sha256:b22121c85def` image and every untagged legacy-builder
image whose metadata referenced the old model stages were deleted explicitly.
A final metadata scan returned zero matches for those stages and paths, so the
baked ONNX assets are no longer present in the local Docker image store. They
are recoverable only by rebuilding or pulling a historical image again.

Removing the transitive ML dependency chain exposed that jarvis-todoist used
`requests` without declaring it. `requests` is now an explicit Todoist and
container runtime dependency rather than arriving accidentally through the
old model stack.

## Hardening discovered during reindex

llama.cpp embedding mode requires physical and micro-batch sizes to match.
Both services now use 8,192 for context, batch, and micro-batch. The vault
chunker also now subdivides oversized single paragraphs and fenced blocks at
line boundaries, with a hard-slice fallback, instead of allowing them to
bypass `max_chunk_chars`.

## Verification

- 1,856 jarvis-core tests passed.
- 99 jarvis-obsidian tests passed.
- 54 memory-explorer tests passed.
- 13 host-inference/LaunchAgent/container-contract tests passed.
- 72 jarvis-todoist tests passed.
