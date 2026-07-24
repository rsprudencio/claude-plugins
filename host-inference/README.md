# Native host inference

This directory runs only Jarvis retrieval inference directly on macOS through
`llama-server` and Metal. PostgreSQL, pgvector, the MCP servers, and the rest
of Jarvis remain containerized.

The initial proof and production cutover evidence are recorded in
[PROOF.md](PROOF.md). The production image contains no inference runtime or
model weights; only the native host services perform retrieval inference.

## Models

| Role | Model | Format | Port |
| --- | --- | --- | --- |
| Embedding | `ibm-granite/granite-embedding-small-english-r2` | F16 GGUF, 384d | 8751 |
| Reranking | `BAAI/bge-reranker-v2-m3` | Q8_0 GGUF | 8752 |

Granite uses CLS pooling, matching its official SentenceTransformers pooling
configuration. Both servers use a single inference slot, an 8,192-token
physical batch/context, and bind only to loopback.

The manifest pins the GGUF repository revision, filename, size, and SHA-256.
The GGUF files are third-party conversions of the original IBM and BAAI
models; the smoke test verifies the runtime contract, and the production
cutover added empirical similarity plus real retrieval validation.

## Prerequisites

Install a recent native Apple Silicon build of llama.cpp:

```sh
brew install llama.cpp
```

Check the environment without changing anything:

```sh
python3 host-inference/host_models.py doctor
```

## Fetch the pinned models

Models are stored outside the repository under
`~/.jarvis/models/llama.cpp` by default:

```sh
python3 host-inference/host_models.py fetch
```

Set `JARVIS_HOST_MODEL_DIR` to use another location.

## Run the proof

Start each service in its own terminal. Both bind only to host loopback.

```sh
python3 host-inference/host_models.py serve granite_embedding
```

```sh
python3 host-inference/host_models.py serve bge_reranker
```

Then run the database-free contract and semantic smoke test:

```sh
python3 host-inference/host_models.py smoke
```

The embedding check requires a finite, L2-normalized 384-dimensional vector.
The reranking check requires the memory-retrieval passage to rank first.

## Run persistently with launchd

After the foreground smoke test passes, install two user LaunchAgents:

```sh
python3 host-inference/launchd.py install
python3 host-inference/launchd.py status
```

Logs are written under `~/.jarvis/logs/model-host`. The agents execute the
native `llama-server` binary directly, restart after failures, and remain
independent from Docker and Colima.

To remove only the host services:

```sh
python3 host-inference/launchd.py uninstall
```

## Activate Jarvis

The activation command creates `~/.jarvis/config.json.pre-host-inference.bak`
once, preserves unrelated configuration, and changes only retrieval settings:

```sh
python3 host-inference/configure.py activate
```

Restart the Jarvis container after changing active settings. There is no local
in-container rollback backend. To change inference implementations, point the
host URLs at another compatible service or deploy an older image explicitly;
do not switch this image to `onnx`.

## Reindex existing data

The namespaced reindexer stages and validates every replacement vector before
one atomic live-table update. Run it with a database owner connection because
it briefly locks each table and disables the `updated_at` trigger to preserve
memory timestamps:

```sh
docker exec --user postgres \
  --env 'POSTGRES_URL=postgresql:///jarvis?host=/var/run/postgresql' \
  -w /app/jarvis-core jarvis-jarvis-1 \
  python bin/reindex_embeddings.py --dry-run

docker exec --user postgres \
  --env 'POSTGRES_URL=postgresql:///jarvis?host=/var/run/postgresql' \
  -w /app/jarvis-core jarvis-jarvis-1 \
  python bin/reindex_embeddings.py
```

Vault files can then be force-indexed through the normal `index_vault` tool.
Oversized single paragraphs and fenced blocks are line-split so no chunk can
bypass the configured character ceiling.

To inspect commands without starting a process:

```sh
python3 host-inference/host_models.py serve granite_embedding --print-command
python3 host-inference/host_models.py serve bge_reranker --print-command
```

## Container reachability

After the host smoke test passes, the same endpoints can be probed from a
temporary container without modifying Jarvis:

```sh
docker run --rm curlimages/curl:latest \
  http://host.docker.internal:8751/health
docker run --rm curlimages/curl:latest \
  http://host.docker.internal:8752/health
```

## Production state

The July 21 cutover completed the comparison, Colima reachability, clean
reindex, and BGE reranking gates. The final image then removed the obsolete
ONNX runtimes and weights. Uninstalling the LaunchAgents would therefore make
embedding unavailable to this deployment.

`canary-config.json` is a secret-free minimal host-inference configuration for
starting an ephemeral image canary.

The image and Compose file default to `EMBEDDING_BACKEND=host`,
`RERANKING_BACKEND=host`, and enabled reranking. The host model aliases and
URLs remain independently configurable.

## Tests

The isolated tooling and its tests use only the Python standard library:

```sh
python3 -m unittest discover -s host-inference/tests -v
```
