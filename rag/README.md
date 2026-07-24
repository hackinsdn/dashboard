# hisdn-rag

The local RAG assistant behind the dashboard's chat widget. It answers questions
from the deployment's own documentation, in the user's language, with citations
— and refuses when the answer is not in the corpus.

Design and rationale: [../doc/rag-assistant-design.md](../doc/rag-assistant-design.md).

It runs as its own container so it can be scaled or moved independently, and so
the dashboard image never carries model weights. The dashboard is a thin client
with a timeout, a circuit breaker and a fallback to human support; if this
service is down, the chat widget degrades to a normal support case.

## Layout

| Module | Responsibility |
| --- | --- |
| `ragsvc/config.py` | every knob, from the environment; defaults are the CPU profile |
| `ragsvc/chunking.py` | heading-aware markdown chunking |
| `ragsvc/embeddings.py` | embedding backends (`sentence_transformers`, `hashing`) |
| `ragsvc/store.py` | SQLite chunk store + exact flat vector search |
| `ragsvc/cache.py` | answer/embedding caches (TTL + LRU) |
| `ragsvc/concurrency.py` | the generation gate: one slot, bounded queue |
| `ragsvc/prompts.py` | per-locale system prompts and context formatting |
| `ragsvc/generate.py` | generation backends (`llamacpp`, `openai_compat`, `none`) |
| `ragsvc/pipeline.py` | retrieve → gate → generate → verify |
| `ragsvc/api.py` | the HTTP surface (FastAPI) |

Only `api.py` needs FastAPI, and the model libraries are imported lazily by the
backend that uses them, so the retrieval and grounding logic is unit-testable
with no weights and no heavy dependencies:

```bash
pytest rag/tests -v
```

(The HTTP tests skip themselves when FastAPI is not installed.)

## HTTP API

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | liveness/readiness; no token |
| `POST /v1/answer` | `{question, locale, history, conversation_id}` → `answered` \| `refused`, or `503` when saturated |
| `POST /v1/ingest` | batch upsert of documents (+ a `prune` manifest) |
| `GET /v1/stats` | corpus, cache, gate and latency counters |

All but `/healthz` require `Authorization: Bearer $RAG_SERVICE_TOKEN`.

**Refusals are `200`, not errors.** A `503` means *saturated or broken* and is
what the dashboard's fallback keys on — the two outcomes are shown to the user
differently, so they must not be conflated on the wire. The user-facing wording
of both is rendered by the dashboard through Flask-Babel, not here: the model is
never trusted to produce a correct pt-BR error message, and adding a locale to
the UI must not require redeploying this container.

## Configuration

Defaults are the **CPU-constrained profile**. The accelerated column is what a
DGX Spark (or any GPU host running vLLM/Ollama/TGI) allows.

| Variable | CPU default | Accelerated | Meaning |
| --- | --- | --- | --- |
| `RAG_DATA_DIR` | `/data` | — | where `index.sqlite3` lives |
| `RAG_SERVICE_TOKEN` | — | — | bearer token; empty disables auth (dev only) |
| `RAG_EMBED_BACKEND` | `sentence_transformers` | same | or `hashing` (dev/tests only — it retrieves badly) |
| `RAG_EMBED_MODEL_PATH` | — | — | local path to a small **multilingual** model |
| `RAG_LLM_BACKEND` | `llamacpp` | `openai_compat` | or `none` for retrieval-only |
| `RAG_LLM_MODEL_PATH` | — | — | local GGUF (quantized, 3B-class) |
| `RAG_LLM_BASE_URL` | — | `http://dgx:8000` | OpenAI-compatible endpoint |
| `RAG_MAX_CONCURRENCY` | `1` | `8`–`32` | generation slots |
| `RAG_QUEUE_MAX` | `4` | `64` | admitted-but-waiting requests; overflow → `503` |
| `RAG_TOP_K` | `4` | `8`–`12` | chunks retrieved |
| `RAG_MIN_SCORE` | `0.35` | same | below this the service refuses without generating |
| `RAG_SAME_LANG_BONUS` | `0.05` | same | nudge toward chunks in the user's language |
| `RAG_MAX_PROMPT_TOKENS` | `1600` | `6000` | prefill time dominates CPU latency |
| `RAG_MAX_OUTPUT_TOKENS` | `320` | `1024` | |
| `RAG_GEN_TIMEOUT_S` | `40` | `20` | wall clock; streamed generation is cut short |
| `RAG_CACHE_TTL_S` | `86400` | same | answer cache TTL |
| `RAG_LOG_QUERIES` | `none` | same | `none` \| `hashed` \| `full` — see below |

`RAG_MIN_SCORE` is the single most important knob: it is what turns "I don't
know" into the default rather than a hallucination. Tune it per embedding model
by watching the refusal rate in the dashboard's admin panel.

### A note on the two models

The **embedding** model (`RAG_EMBED_MODEL_PATH`) and the **generation** model
(`RAG_LLM_MODEL_PATH`) are different things in different formats:

- Embeddings is a **sentence-transformers directory** (with `config.json`,
  `modules.json`, `1_Pooling/`). Create it on a networked machine with
  `SentenceTransformer('intfloat/multilingual-e5-small').save('models/embed')`
  and mount the folder — a bare GGUF or a partial file download will fail with
  *"Unrecognized model ... should have a model_type key"*.
- Generation is a **quantized GGUF** (e.g. `qwen2.5-3b-instruct-q4_k_m.gguf`).

Prefer a small **non-reasoning** instruct model on CPU (3B-class). If you do
point it at a hybrid reasoning model (Qwen3, DeepSeek-R1, ...), its
`<think>...</think>` scratchpad is stripped automatically before the answer is
returned (`strip_reasoning` in `generate.py`), so it never reaches the chat and
never derails the grounding check. Note that on CPU such a model may spend its
whole `RAG_GEN_TIMEOUT_S` budget thinking and get cut off before answering,
which then reads as a refusal — a reason to keep to a non-reasoning model unless
you are on an accelerated backend.

## Privacy

- The container needs **no egress**. Enforce it (a `NetworkPolicy` with no
  egress, or an internal-only network); the offline env vars in the Dockerfile
  stop libraries from reaching a model hub, but only the network policy is a
  guarantee.
- Weights are baked into the image or mounted — never fetched at runtime.
- The dashboard sends the question, the locale and a **pseudonymous**
  conversation hash. No user id, name, e-mail or IP ever reaches this service.
- `RAG_LOG_QUERIES=none` (default) logs counters only. `hashed` adds a question
  hash and the matched document ids — enough to find frequently-refused topics
  without storing text. `full` stores question and answer text and is intended
  for staging; using it in production must be disclosed to users.

## Corpus

The index is built by the dashboard's CLI, which owns the documents:

```bash
flask --app run.py cli rag-ingest
```

It posts documents to `/v1/ingest` in batches with a `content_hash` (unchanged
documents are skipped, not re-embedded) and a `prune` manifest per source, so a
run converges the index to the current corpus. Changing the embedding model is
detected automatically and forces a full re-embed.
