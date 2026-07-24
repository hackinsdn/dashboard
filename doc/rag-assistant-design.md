# RAG Assistant — High-Level Design

> **Status: phase 1 is implemented** (`RAG_ENABLED=False` by default, so nothing changes
> until a deployment turns it on). The service lives in [../rag/](../rag/README.md); the
> dashboard side is `apps/controllers/rag_client.py`, the assistant helpers in
> `apps/controllers/support.py`, the endpoints in `apps/api/routes.py`, the corpus builder
> in `apps/cli/rag_ingest.py`, and the tests in `tests/test_rag_assistant.py` +
> `rag/tests/`. Where the built system differs from the plan below, the text has been
> updated to describe what exists.

## Overview

Today the chat widget is a **human support-ticket** channel: the user writes, staff
answers (see [support-chat-design.md](./support-chat-design.md)). This design extends it
with a second answering path — a **local LLM assistant grounded on the deployment's own
documentation (RAG)** — and an explicit routing step at the start of every conversation:

> *"Do you want to open a support case, or do you have a question about HackInSDN?"*

Choosing **support case** keeps the existing behaviour untouched (staff replies, batched
e-mails, admin pages). Choosing **question** sends the message to the RAG assistant, which
answers **in the user's language**, **with citations**, and **refuses when it cannot find
the answer** in the indexed corpus — offering to hand the conversation over to a human.

The model and the retrieval stack run in a **separate container** (`hisdn-rag`) that speaks
a small HTTP contract. The dashboard never imports a model library, never downloads weights
and never holds an index; it is a thin client with a timeout and a fallback. That separation
is what lets the assistant be scaled independently, moved to another host, or repointed at a
GPU box (DGX Spark or similar) without touching the dashboard image.

The **primary target is a CPU-only server**, which is the hard constraint that shapes almost
every choice below: a small quantized model, a single generation slot, aggressive caching,
short contexts, and a fallback that is never worse than today's behaviour (a human case).

---

## Requirements

### Functional

1. When a **new conversation** starts, the widget asks the user to pick: **open a support
   case** or **ask a question**. The choice is persisted on the thread and drives which
   backend answers.
2. **Support case** → the current human flow, unchanged.
3. **Question** → the RAG assistant answers from the local corpus.
4. Answers are produced in the **language chosen by the user** (`en` or `pt_BR`, following
   the existing locale resolution), regardless of the language of the source documents.
5. **Refusal when the information is not found** — no answers invented outside the retrieved
   context; a refusal offers escalation to human support.
6. **Source citations** on every non-refusal answer, pointing at the ingested documents.
7. **Local document ingestion** — a repeatable, incremental pipeline over the deployment's
   own documentation, lab guides and lab descriptions.
8. **Escalation**: at any point (and always after a refusal) the user can convert the
   conversation into a normal support case; the existing staff notification path takes over.
   The escalation captures **telemetry** (the page the user was on, browser/User-Agent, IP)
   *at the moment of the hand-over*, plus the assistant context that led to it, and reports
   it on the support case.
9. **Feedback on every assistant answer** (👍/👎, with an optional reason on 👎), collected
   from the first release — it is the deployment's only real signal about answer quality,
   and it is worthless if it starts being collected later than the answers themselves.

### Non-functional

10. The RAG/LLM stack runs as a **separate container**, reachable over HTTP, so it can be
    scaled or relocated independently of the dashboard.
11. Designed for a **CPU-constrained** host: small **quantized** model, **strict concurrency
    limits**, **answer caching**, **short context windows**, **graceful fallback**.
12. **Extensible to accelerated deployments** (DGX Spark or similar) by configuration only —
    no code changes in the dashboard, no change to the HTTP contract.
13. **Local-only privacy**: no request, document or telemetry leaves the deployment; model
    and embedding weights are baked into the image or mounted, never fetched at runtime.
14. **Logging controls**: what is logged about a question (nothing / hashed / full) is a
    deployment decision, with a conservative default.

---

## Architecture

### Components

```
                 ┌──────────────────────────────────────────┐
   browser ────► │  dashboard (Flask, gunicorn+gevent)      │
   chat widget   │                                          │
                 │  apps/controllers/support.py             │
                 │    generate_support_reply()  ── seam     │
                 │  apps/controllers/rag_client.py          │
                 │    HTTP client + circuit breaker         │
                 └───────────────┬──────────────────────────┘
                                 │  HTTP + bearer token
                                 │  (RAG_SERVICE_URL)
                 ┌───────────────▼──────────────────────────┐
                 │  hisdn-rag container (FastAPI)           │
                 │                                          │
                 │  /v1/answer   retrieve → prompt → gen    │
                 │  /v1/ingest   chunk → embed → upsert     │
                 │  /healthz /v1/stats                      │
                 │                                          │
                 │  ┌────────────┐   ┌────────────────────┐ │
                 │  │ embeddings │   │ generation backend │ │
                 │  │ e5-small   │   │  llamacpp (CPU)    │ │
                 │  │ (ONNX/CPU) │   │  or openai-compat  │─┼──► optional
                 │  └─────┬──────┘   └────────────────────┘ │    vLLM/Ollama
                 │        │            answer cache (LRU)   │    on DGX Spark
                 │  ┌─────▼──────────────────────────────┐  │
                 │  │ vector index (sqlite-vec / FAISS)  │  │
                 │  │ + chunk store   — mounted volume   │  │
                 │  └────────────────────────────────────┘  │
                 └──────────────────────────────────────────┘
```

The dashboard's only dependency on the assistant is the HTTP contract; if the service is
down, slow or saturated, the dashboard degrades to the human path.

### Why a separate container (and not a library)

- Model weights (~2–3 GB) must not bloat the dashboard image, which is rebuilt often.
- CPU-bound generation inside a gunicorn worker would starve the (single, gevent) web
  worker; in a separate process it is bounded by its own scheduler and can be pinned to
  specific cores or a specific host.
- Independent scaling: 1 dashboard replica may face N assistant replicas, or the assistant
  may live on a beefier node while the dashboard stays where it is.
- Swapping the generation backend (CPU quantized → GPU server) becomes a deployment
  concern, not a code change.

---

## Conversation flow

### Thread modes

`SupportThreads` gains a `mode` column: `support` (human, default and legacy value) or
`assistant` (RAG). The choice is made once per thread:

```
user opens widget, no active thread
        │
        ▼
 ┌────────────────────────────────────────────┐
 │  How can we help?                          │
 │   [ Open a support case ]  [ Ask a question ] │
 └────────────────────────────────────────────┘
        │                            │
   mode=support                 mode=assistant
        │                            │
   existing flow            POST message → 201
   (staff reply,            widget shows "typing…"
    batched e-mail)         POST /assistant/answer (blocking, ≤ RAG_TIMEOUT)
                                     │
                       ┌─────────────┴──────────────┐
                    answer + citations           refusal / fallback
                             │                        │
                     [ 👍 ] [ 👎 ]                     │
                    [ Talk to a human ]      [ Talk to a human ] (prominent)
                             │                        │
                             └──────► mode=support, system message,
                                      escalation telemetry snapshot,
                                      staff notified via the batched job
```

Key properties:

- **The mode is a thread property, not a message property**, so the admin pages, the user
  pages and the e-mail batching keep working unchanged; an assistant thread simply has no
  pending staff work until it is escalated.
- **Escalation is one-way** (`assistant → support`) and records a `system` message
  (*"Conversation handed over to the support team."*) carrying a **telemetry snapshot** (see
  below). The full assistant transcript is visible to staff, which is exactly the context
  they need.
- Assistant threads are **excluded from the support e-mail batch and from the admin
  open-cases badge** until escalated — otherwise staff would be paged for every question.
  This is a filter on `mode == "support"` in `open_thread_count()` and in
  `flush_support_emails`.
- If the assistant is **disabled or unreachable at thread-creation time**, the chooser is
  not rendered at all and the widget behaves exactly as today.

### Why the answer is fetched by a second request

CPU generation takes seconds, not milliseconds. Rather than introducing a task queue, the
widget posts the user message (fast, `201`, message persisted) and then calls a dedicated
endpoint that performs the RAG round-trip and returns the assistant message. Consequences:

- The user message is **never lost**, even if generation fails afterwards.
- The request is held open by a **gevent** worker (gunicorn's gevent worker monkey-patches,
  so the `requests` call to `hisdn-rag` yields instead of blocking the worker) — 128 threads
  worth of concurrency is far more than the assistant will ever accept.
- A "typing…" indicator and a client-side timeout give an honest UX.
- Token **streaming** (SSE from `hisdn-rag`, relayed by the dashboard) is a drop-in
  evolution of this same endpoint — see *Future work*.

### Escalation telemetry

The support chat already captures telemetry (`origin_page`, `user_agent`, `ip_address`) —
but only **once, when the thread is created** (`record_telemetry`, called from
`POST /support/thread/messages` on a new thread). That is the wrong moment for an escalated
assistant conversation: the thread may have started on the dashboard home page half an hour
and six questions earlier, while the page the user was actually stuck on — the one staff
needs — is wherever they were when they pressed **"Talk to a human"**.

So escalation takes its **own snapshot**, without discarding the original one:

| Field | Source | Why staff wants it |
| --- | --- | --- |
| `page` | sent by the widget (`window.location.pathname` + document title), same convention as the message POST | the screen the user was stuck on |
| `user_agent` | `User-Agent` header at escalation time | may differ from thread start (different device/session) |
| `ip_address` | `get_remote_addr()` at escalation time | may differ (moved networks, campus vs. home) |
| `locale` | `SupportThreads.locale` | which language to answer in |
| `questions_asked` / `refusals` / `negative_feedback` | counted from the thread's messages | *"asked 4 things, got 3 refusals"* is the whole story in one line |
| `last_question` | the last user message | what they actually wanted |
| `assistant_available` | breaker/health state at the time | distinguishes *"the assistant couldn't help"* from *"the assistant was down"* |

The snapshot is stored as the `meta` of the `system` escalation message — the JSON column
already introduced for citations — so **no additional schema is needed** and the telemetry
sits at exactly the point in the transcript where it applies. If the thread's original
telemetry fields are empty (a thread that never went through the create-with-telemetry
path), escalation backfills them too, so the existing telemetry blocks are never blank.

It surfaces in three places, all of which already render thread telemetry today:

- the **admin thread view** — a second telemetry block anchored at the escalation note
  (*"Escalated from: /labs/… — after 4 questions, 3 refusals"*), alongside the existing
  "Started from" block;
- the **batched support e-mail** (`templates/mail/support_message.html`) — the same block,
  plus the last question and the assistant's final reply, so staff can triage from the
  inbox without opening the dashboard;
- the **user's own thread view** — not shown; telemetry is staff-facing.

Escalation telemetry is **dashboard-side only**. None of it is ever sent to `hisdn-rag`,
which continues to see only the question, the locale and a pseudonymous conversation hash.

---

## The `hisdn-rag` service

A small FastAPI application. Stateless except for the read-mostly index on a mounted volume.

### `POST /v1/answer`

```jsonc
// request
{
  "question": "How do I extend the expiration date of my lab?",
  "locale": "pt_BR",              // en | pt_BR — the user's chosen language
  "history": [                     // optional, last N turns, already truncated
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "conversation_id": "sha256:...", // pseudonymous, for logs/rate limits — never the user id
  "top_k": 4                       // optional override
}
```

```jsonc
// 200 — grounded answer
{
  "status": "answered",
  "answer": "Para estender a validade do seu lab, abra ...",
  "sources": [
    {"title": "Lab scheduling", "url": "/doc/DEV.md#lab-scheduler", "score": 0.81,
     "snippet": "…", "lang": "en"},
    {"title": "Guia do Lab: BGP Hijacking", "url": "/labs/3f2a…", "score": 0.74,
     "snippet": "…", "lang": "pt_BR"}
  ],
  "cached": false,
  "usage": {"prompt_tokens": 1180, "completion_tokens": 210, "latency_ms": 9400}
}

// 200 — refusal (retrieval below threshold, or the model could not ground the answer)
{"status": "refused", "reason": "no_relevant_context", "sources": []}

// 503 — saturated / unhealthy; the dashboard falls back
{"status": "unavailable", "reason": "queue_full", "retry_after_s": 20}
```

`status` is the contract, not the prose: the **user-facing refusal and fallback texts are
rendered by the dashboard** through Flask-Babel, so they match the rest of the UI and stay
translatable without redeploying the model container.

### `POST /v1/ingest`

Batch upsert of documents; the service chunks, embeds and indexes them.

```jsonc
{
  "documents": [
    {"doc_id": "doc/DEV.md",            // stable, unique — the upsert key
     "title": "Developer guide",
     "url": "https://…/doc/DEV.md",     // used for citations
     "lang": "en",                      // en | pt_BR | auto
     "source": "repo-docs",             // corpus partition
     "content_hash": "sha256:…",        // skip re-embedding when unchanged
     "text": "# Developer guide\n…"}
  ],
  "prune": {"source": "repo-docs", "keep_doc_ids": ["doc/DEV.md", "…"]}
}
```

`prune` makes deletions explicit: any chunk of that `source` whose `doc_id` is absent is
removed, so an ingestion run converges the index to the current corpus.

### `GET /healthz`, `GET /v1/stats`

Liveness/readiness (model loaded, index openable) and operational counters: documents,
chunks, cache hit ratio, queue depth, p50/p95 latency, refusal rate. `/v1/stats` feeds an
admin panel (see *Dashboard changes*).

### Auth

Bearer token (`RAG_SERVICE_TOKEN`), plus network-level restriction. The service is never
exposed to end users — only the dashboard calls it.

---

## The CPU-constrained profile

This is the default profile and the one the system must be correct under.

### Model choices

| Role | Default (CPU) | Notes |
| --- | --- | --- |
| Generation | a **3B-class instruction model, 4-bit quantized** (GGUF `Q4_K_M`) with strong pt-BR + en coverage | ~2 GB RSS; ~8–15 tok/s on 4–8 modern cores |
| Embeddings | a **small multilingual embedding model** (384-dim class, ONNX/CPU) | pt-BR and en land in the same space, which is what makes cross-language retrieval work |
| Reranking | **none** on CPU | a cross-encoder would double the latency budget; the score threshold does the filtering |

Concrete model identifiers are deployment configuration (`RAG_LLM_MODEL_PATH`,
`RAG_EMBED_MODEL_PATH`), not code, so a deployment can trade quality for latency without a
rebuild. The corpus is small (order of 10³ chunks), so the *retrieval* side is never the
bottleneck — a flat exact index is both faster and simpler than an approximate one.

### Strict concurrency limits

- **One generation slot** (`RAG_MAX_CONCURRENCY=1`). Two concurrent generations on the same
  cores roughly double both latencies; serializing is strictly better for p95.
- A **bounded FIFO queue** (`RAG_QUEUE_MAX=4`) in front of the slot. Full queue → immediate
  `503 queue_full`; the dashboard falls back rather than making the user wait behind three
  other people.
- **Per-request wall clock** (`RAG_GEN_TIMEOUT_S=40`) and **max output tokens**
  (`RAG_MAX_OUTPUT_TOKENS=320`) — a runaway generation is cancelled, not tolerated.
- **Per-user rate limit** on the dashboard side (`RAG_USER_RATE_LIMIT`, default 10 questions
  per 10 min) so one user cannot hold the single slot indefinitely.
- Retrieval and embedding run outside the generation slot (they are cheap and
  parallelizable), so a queued request is already retrieved by the time the slot frees up.

### Answer caching

Two caches, both in-process with a size cap, and both **partitioned by locale**:

| Cache | Key | TTL | Effect |
| --- | --- | --- | --- |
| Answer cache | `sha256(normalized_question + locale + index_version)` | `RAG_CACHE_TTL_S` (default 24 h) | repeated FAQs cost ~0 ms |
| Embedding cache | `sha256(normalized_question)` | same | saves ~50–150 ms per repeat/near-repeat |

Normalization = lowercase, whitespace collapse, trailing punctuation strip. `index_version`
is bumped by every ingestion run, which invalidates stale answers for free — no manual
cache flush after re-ingesting. Cached answers are returned with `"cached": true` and are
**not** re-cited from scratch (citations are cached alongside the answer).

Optionally the answer cache can be backed by the deployment's Redis (`CACHE_REDIS_URL`
already exists for the dashboard) so multiple `hisdn-rag` replicas share it. In-process is
the default because the single-container case is the common one.

### Short context windows

| Knob | Default | Rationale |
| --- | --- | --- |
| `RAG_TOP_K` | 4 | prompt length dominates CPU prefill time |
| `RAG_CHUNK_TOKENS` | 400 (≈ 60 token overlap) | heading-aware chunks, so a chunk is usually self-contained |
| `RAG_MAX_PROMPT_TOKENS` | 1600 | context is truncated chunk-by-chunk, lowest score dropped first |
| `RAG_HISTORY_TURNS` | 2 | enough for "and how do I undo that?", cheap enough to always send |
| `n_ctx` | 4096 | leaves headroom for prompt + output without paging |

Prefill on CPU is roughly linear in prompt tokens, so these caps are the main latency lever:
1600 prompt tokens + 320 output tokens is a ~10–20 s answer on a typical 8-core server —
slow, but honest, and cached afterwards.

### Graceful fallback

Every failure mode collapses into *the user is offered a human*, never into an error:

| Condition | Dashboard behaviour |
| --- | --- |
| `503 queue_full` / `unavailable` | assistant message: *"I'm busy right now — try again in a moment, or open a support case."* + escalate button |
| HTTP timeout (`RAG_TIMEOUT_S`, default 45 s) | same, worded as *"took too long"* |
| Connection error / service down | same; **circuit breaker** opens |
| `status: refused` | the localized refusal text + escalate button (prominent) |
| Assistant disabled (`RAG_ENABLED=False`) | the mode chooser is not shown at all |

A **circuit breaker** in `rag_client.py` (N consecutive failures → open for M seconds,
half-open probe) keeps the widget snappy when the service is down: while open, the chooser
hides the assistant option and the dashboard does not attempt the call.

Crucially, the fallback never loses data: the user's message is already persisted, so
escalating turns it into a normal support case with zero retyping.

### Latency budget (CPU, indicative)

| Stage | Budget |
| --- | --- |
| embed question | 50–150 ms |
| vector search (flat, ~10³ chunks) | < 20 ms |
| prompt assembly | < 10 ms |
| generation (1600 in / ~250 out) | 8–20 s |
| **total, cold** | **~10–20 s** |
| **total, cached** | **< 50 ms** |

---

## Grounding, refusal and citations

### Retrieval gate

Before the model is invoked, retrieval must clear a bar: the best chunk's similarity must
exceed `RAG_MIN_SCORE` (default 0.35 on cosine, tuned per embedding model). Below that, the
service returns `refused / no_relevant_context` **without spending a generation slot** —
which is also the cheapest possible answer, and the common case for off-topic questions.

### Prompting

The system prompt (one per locale, shipped with the service) enforces three rules:

1. Answer **only** from the numbered context blocks.
2. If the context does not contain the answer, reply with the exact sentinel `NO_ANSWER`.
3. Answer in the target language, and cite the blocks used as `[1]`, `[2]`.

Generation uses a low temperature (`RAG_TEMPERATURE=0.2`) — this is a lookup task, not a
creative one.

### Post-generation check

The sentinel alone is not trusted. The service also refuses when:

- the answer contains `NO_ANSWER` (or its localized variants), **or**
- the answer cites no block, **or**
- the answer is suspiciously long relative to the retrieved context (a hallucination tell).

Only blocks actually cited are returned in `sources`, capped at
`RAG_MAX_CITATIONS` (default 3) and de-duplicated by `doc_id`.

### Citation rendering

Assistant messages carry their citations in a new `SupportMessages.meta` JSON column
(`{"sources": [...], "cached": true, "latency_ms": 9400}`). The widget renders them under
the bubble as a compact numbered list of links; the admin and user thread pages render the
same list, so staff reviewing an escalated case sees exactly what the user was told and on
what basis.

`meta` being a nullable JSON column keeps all existing senders and rows untouched.

---

## Answer feedback

Every assistant answer carries **👍 / 👎 buttons**, from the first release. This is not a
nice-to-have deferred to a later phase: feedback can only be collected at the moment the
user reads the answer, so shipping the assistant without it means the entire pilot produces
no quality signal — and the pilot is precisely the phase where that signal decides whether
to widen the rollout, change the model, or write more documentation.

### What is collected

| Field | Notes |
| --- | --- |
| `feedback` | `up` \| `down`, nullable — no vote is the normal case |
| `feedback_reason` | on 👎 only: one of a small localized set, or free text |
| `feedback_at` | when it was given |

These are **dedicated columns on `support_messages`**, not keys inside `meta`. Feedback is
the one part of the assistant that gets aggregated in SQL (rates per week, per source, per
locale), and portable JSON querying across SQLite/MySQL/PostgreSQL is not worth the trouble
for three scalar fields. Citations stay in `meta` because they are only ever read back
whole, with the message.

The 👎 reasons are a fixed, translatable list — free text is optional and secondary, because
categories are what make the data aggregable:

- *Wrong or misleading answer*
- *Not related to what I asked*
- *Incomplete — it stopped short*
- *Hard to understand*
- *Other* (free text)

### Behaviour

- Voting is **idempotent and reversible**: a second click on the same button clears the
  vote, the other button switches it. One vote per message, by the thread owner only.
- A 👎 immediately surfaces the **"Talk to a human"** button in the same emphasized form
  used after a refusal — a bad answer and a refusal are the same problem from the user's
  point of view, and the escalation carries the negative vote in its telemetry snapshot.
- A 👍 does nothing beyond recording; no interstitial, no thank-you modal.
- Votes are visible on the **admin and user thread views** (read-only for staff), so an
  escalated case shows staff which answers the user rejected.
- Feedback is **never sent to `hisdn-rag`** and never leaves the deployment. It is joined to
  the question and citations locally, by message id.

### What it feeds

The admin "Assistant" panel reports **thumbs-down rate** next to refusal rate, and lists
recent 👎 answers with their question, reason and cited sources. Those two numbers split the
failure modes cleanly and point at different fixes:

| Signal | Diagnosis | Fix |
| --- | --- | --- |
| High refusal rate | the corpus does not cover what users ask | write documentation / FAQ entries |
| High 👎 rate on answered questions | retrieval or the model is the problem | tune `RAG_MIN_SCORE` / `RAG_TOP_K`, or a bigger model on an accelerated host |
| Both low, few escalations | working as intended | widen the rollout |

Down-voted answers with their questions also form the seed of a **regression set**: a small
file of question → expected-source pairs that can be replayed against the service after a
model, prompt or chunking change. That is the cheapest evaluation harness available, and it
only exists if the buttons ship with the feature.

---

## Language handling

The dashboard resolves the locale exactly as it does for the UI (see
[i18n.md](./i18n.md)): `session["locale"]` → `Users.locale` → `Accept-Language` →
`BABEL_DEFAULT_LOCALE`. That resolved value is sent as `locale` on every `/v1/answer` call
and stored on the thread (`SupportThreads.locale`) so an escalated case tells staff which
language the user was working in.

Inside the service:

- **Retrieval is cross-language by design.** The multilingual embedding model puts a
  pt-BR question near an English chunk about the same topic, so an en-only corpus still
  answers pt-BR questions. Chunks in the user's language get a small score bonus
  (`RAG_SAME_LANG_BONUS`, default +0.05) so translated documents win when they exist.
- **Generation is single-language.** The per-locale system prompt instructs the model to
  answer in the target language *even when the context is in another language*, which is
  the normal case for this corpus.
- **Refusals, fallbacks and UI strings come from the dashboard's catalogs**, not from the
  model — the assistant never has to be trusted to produce a correct pt-BR error message,
  and adding a third locale is a catalog change plus one system prompt.
- Citation titles keep the **source document's** language and are marked with their `lang`,
  so a pt-BR answer citing an English page is not confusing.

---

## Local document ingestion

### Sources

| Source id | Content | Where from |
| --- | --- | --- |
| `repo-docs` | `doc/*.md`, `README.md` | the checked-out repository |
| `lab-guides` | `Labs.lab_guide_md` per lab | the database |
| `lab-descriptions` | `Labs.title`, `description`, `extended_desc`, `goals`, categories | the database |
| `lab-templates` | `README`/docs from the lab-templates git checkout (`LAB_TEMPLATES_DIR`) | already synced on disk |
| `faq` | a curated `doc/faq/*.md` (en + pt_BR) written for the assistant | the repository |

Only content the user is allowed to see in aggregate is ingested. **No user data, no
support transcripts, no manifests with credentials** — the corpus is public-facing product
documentation. Per-lab access control is *not* modelled in the index; instead, the
`lab-guides` source is opt-in (`RAG_INGEST_SOURCES`) for deployments where lab guides are
considered restricted.

### Pipeline

Ingestion is driven from the dashboard (it is the side that has the database and the repo)
and executed by the service (it is the side that has the embedding model):

```
flask --app run.py cli rag-ingest [--source repo-docs] [--dry-run] [--full]
        │
        ├─ collect documents from the enabled sources
        ├─ normalize: markdown → text, strip HTML/front-matter, drop code-only blocks
        ├─ compute content_hash per document
        ├─ POST /v1/ingest in batches of RAG_INGEST_BATCH (default 32)
        │     service: skip unchanged hashes → chunk → embed → upsert
        └─ POST prune manifest → service deletes chunks of removed documents
```

- **Chunking** is heading-aware: split on markdown headings first, then pack to
  `RAG_CHUNK_TOKENS` with overlap; each chunk inherits its document title plus its heading
  path (*"Developer guide › Scheduled jobs (cron)"*), which is what makes citations
  readable and retrieval precise.
- **Incremental by default**: unchanged `content_hash` costs one comparison, not one
  embedding. A full corpus re-embed is the `--full` escape hatch (and what a new embedding
  model requires).
- **Document-atomic writes**: each document is replaced (chunks deleted and re-inserted)
  inside one SQLite transaction, and the run ends with an `index_version` bump that
  invalidates the answer cache. A failed run therefore leaves a mix of old and new
  documents rather than a half-written one — every document in the index is internally
  consistent, and the next run converges the rest. A shadow index that swaps atomically
  across the *whole* corpus would be stronger; it is not worth the complexity at this
  corpus size, where a full rebuild is a couple of minutes.
- **Prune is per source and rides the final batch**, so a partially-sent corpus never
  deletes live documents.
- **Scheduling**: a cron entry alongside the existing jobs (see
  [DEV.md](./DEV.md#scheduled-jobs-cron)) — hourly is generous for a corpus that changes on
  deploys and lab edits.

`--dry-run` prints what would be sent (documents, chunk counts, deletions) and changes
nothing, which is also how a deployment audits what the assistant knows.

---

## Privacy and logging controls

**Local-only is enforced, not assumed.**

- Weights (LLM + embeddings) are **baked into the `hisdn-rag` image or mounted from a
  volume**; the container runs with `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` and
  telemetry env vars disabled, so no library "phones home" on first use.
- The service needs **no egress**; deployments should enforce that (a Kubernetes
  `NetworkPolicy` allowing only ingress from the dashboard, no egress). This is documented
  as part of the install steps, because it is the only real guarantee.
- The dashboard sends **the question, the locale, and a pseudonymous conversation hash** —
  never the user id, e-mail, name or IP. Everything staff needs to correlate is already in
  the dashboard's own database.
- The corpus contains **no user data** (see *Sources*).
- **Feedback votes and escalation telemetry never leave the dashboard.** Both are joined to
  the question locally, by message id; `hisdn-rag` is never told that an answer was
  down-voted or which page a user escalated from.

**Logging is a deployment choice** via `RAG_LOG_QUERIES`:

| Value | Logged by `hisdn-rag` |
| --- | --- |
| `none` (default) | counters only: latency, cache hit, refusal, queue depth — no text |
| `hashed` | the above + `sha256(normalized_question)` and matched `doc_id`s, so operators can find *which* questions are frequent or frequently refused, without storing text |
| `full` | the above + question and answer text, for tuning; intended for staging, and must be announced to users if used in production |

Independently, `RAG_STORE_TRANSCRIPTS` (default `True`) controls whether assistant Q&A is
persisted in `SupportMessages` on the dashboard side. It is on by default because an
escalated case is useless to staff without the transcript — but a deployment with stricter
rules can set it to `False`, in which case assistant answers are shown in the widget for
the session only and escalation carries just the user's messages. This is the one privacy
knob that changes user-visible behaviour, so it is documented prominently — and it has a
real cost: with no persisted assistant message there is nothing to attach a vote to, so
**feedback collection is disabled together with transcripts**, and the deployment gives up
its only quality signal. The widget hides the 👍/👎 buttons in that mode rather than
collecting votes it cannot store.

Retention of assistant threads follows whatever policy already applies to support threads;
no new retention mechanism is introduced.

---

## Scaling and the accelerated (DGX Spark) profile

The extension point is the **generation backend**, selected by `RAG_LLM_BACKEND`:

| Backend | Meaning | Typical use |
| --- | --- | --- |
| `llamacpp` (default) | in-process quantized model, CPU | single small server |
| `openai_compat` | HTTP call to an external OpenAI-compatible endpoint (`RAG_LLM_BASE_URL`) | vLLM / Ollama / TGI on a DGX Spark or any GPU host |
| `none` | retrieval only (returns top chunks, no prose) | debugging, or an ultra-constrained host |

Moving to a GPU host is therefore: point `RAG_LLM_BASE_URL` at it, pick a bigger model, and
relax the constraints that only existed because of the CPU:

| Knob | CPU default | Accelerated |
| --- | --- | --- |
| `RAG_MAX_CONCURRENCY` | 1 | 8–32 |
| `RAG_QUEUE_MAX` | 4 | 64 |
| `RAG_TOP_K` | 4 | 8–12 |
| `RAG_MAX_PROMPT_TOKENS` | 1600 | 6000+ |
| `RAG_MAX_OUTPUT_TOKENS` | 320 | 1024 |
| reranker | off | on (cross-encoder) |
| `RAG_TIMEOUT_S` (dashboard) | 45 | 20 |

No dashboard code changes, no contract changes — the same `/v1/answer` call, just faster
and with a wider context.

**Horizontal scaling.** `hisdn-rag` is stateless apart from the index, so N replicas behind
a Service work as long as they share an `index_version`. Two supported topologies:

1. **Read-only volume** (default): each replica mounts the same built index; ingestion runs
   against a designated writer, replicas reload on `index_version` change. Simple, fits the
   corpus size.
2. **External vector store** (Qdrant/pgvector) when the corpus or the replica count grows;
   this is why chunk storage sits behind a small repository interface in the service rather
   than being called directly.

---

## Dashboard changes

### Data model (`apps/home/models.py`)

| Table | Change |
| --- | --- |
| `support_threads` | `mode` — `support` \| `assistant`, default `support`, `NOT NULL` |
| `support_threads` | `locale` — the locale the conversation runs in, nullable |
| `support_messages` | `meta` — JSON/Text, nullable: citations, assistant diagnostics, and the escalation telemetry snapshot on `system` messages |
| `support_messages` | `feedback` — `up` \| `down`, nullable |
| `support_messages` | `feedback_reason` — short code or free text, nullable |
| `support_messages` | `feedback_at` — when the vote was cast, nullable |

Migration `2.0.14 → 2.0.15`
(`migrations/versions/2.0.14_add_rag_assistant_2.0.15.py`), backfilling `mode='support'`
for every existing thread, which makes the change invisible to current data. All the new
message columns are nullable, so existing rows need no backfill at all.

### Controllers

- **`apps/controllers/rag_client.py`** (new) — HTTP client: `answer(question, locale,
  history, conversation_id)`, timeout, bearer auth, circuit breaker, `health()`,
  `ingest(documents, prune)`, `stats()`. It returns a small result object
  (`answered | refused | unavailable`), never raises into the request path.
- **`apps/controllers/support.py`** — `generate_support_reply(thread, body)` stays the seam
  but is now only used for the synchronous path; the assistant answer is produced by
  `answer_with_assistant(thread, message)`, which calls the client, renders the localized
  refusal/fallback text, and persists an `assistant` message with `meta`. New helpers:
  `set_thread_mode(thread, mode)`, `escalate_thread(thread)` (mode → `support` + `system`
  message + leaves the message unread so the batch job picks it up),
  `assistant_available()` (config + circuit breaker), `record_feedback(message, vote,
  reason)`, and `assistant_stats(thread)` (questions asked, refusals, negative votes — the
  counters the escalation snapshot needs).
- **`escalate_thread(thread, page, user_agent, ip)`** builds the telemetry snapshot
  described in *Escalation telemetry*, stores it as the `system` message's `meta`, and
  backfills the thread's own telemetry columns when they are empty. It reuses
  `record_telemetry` rather than duplicating the field handling.
- **`open_thread_count()`** and **`flush_support_emails`** filter on `mode == "support"`,
  so unescalated assistant threads never appear as staff work.

### HTTP API (`apps/api/routes.py`)

| Method & path | Who | Purpose |
| --- | --- | --- |
| `POST /support/thread/mode` | user | set the mode of the active/new thread (`support` \| `assistant`) |
| `POST /support/assistant/answer` | user | produce the assistant reply for the thread's last user message; blocks up to `RAG_TIMEOUT_S`; returns the persisted `assistant` message (with `meta.sources`) |
| `POST /support/thread/escalate` | user | convert an assistant thread into a human support case; body carries `page` (the widget sends it exactly as it does when posting a message), `User-Agent` and IP come from the request |
| `POST /support/messages/<id>/feedback` | owner | record 👍/👎 (+ optional reason) on an assistant message; re-posting the same vote clears it |
| `GET /support/assistant/status` | user | whether the assistant is enabled and currently healthy (drives the chooser) |
| `GET /support/assistant/stats` | admin | proxy of `hisdn-rag` `/v1/stats` for the admin page |

Existing endpoints keep their contracts; `POST /support/thread/messages` gains an optional
`mode` on thread creation.

### UI

- **Widget** (`includes/chatbot.html`) — a **mode chooser card** on a fresh conversation
  (hidden when the assistant is unavailable); an assistant **"typing…"** indicator while
  `/assistant/answer` is in flight; **citation lists** under assistant bubbles;
  **👍/👎 buttons** under every assistant answer, with an inline reason picker on 👎; a
  **"Talk to a human"** button, always present in assistant mode and emphasized after a
  refusal, a fallback or a 👎; the poll interval drops to 3 s while an answer is pending and
  returns to 30 s afterwards. All new strings go through `_()` (and `window.i18n` where
  static JS is involved), per [i18n.md](./i18n.md).
- **Thread views** (admin + user) — a badge showing the thread mode, citations rendered
  under assistant messages so staff sees what the assistant claimed and cited, the user's
  👍/👎 (read-only), and — on the escalation note — the **escalation telemetry block**
  next to the existing "Started from" block.
- **Batched e-mail** (`mail/support_message.html`) — the escalation telemetry block and the
  assistant's last question/answer, so an escalated case can be triaged from the inbox.
- **Admin** — an "Assistant" panel (`GET /support/assistant`, sidebar entry shown only when
  `RAG_ENABLED`) rendering `pages/assistant_panel.html` from `/v1/stats` plus locally
  computed feedback:
  corpus size, index version and age, cache hit ratio, refusal rate, **thumbs-down rate**,
  p95 latency, breaker state, and a list of recent 👎 answers with question, reason and
  cited sources. Refusal rate and 👎 rate are the two numbers that decide what to fix.

### Configuration (`apps/config.py`)

| Setting | Default | Meaning |
| --- | --- | --- |
| `RAG_ENABLED` | `False` | master switch; off = today's behaviour exactly |
| `RAG_SERVICE_URL` | — | e.g. `http://hisdn-rag:8080` |
| `RAG_SERVICE_TOKEN` | — | bearer token for the service |
| `RAG_TIMEOUT_S` | `45` | client-side wall clock for `/v1/answer` |
| `RAG_BREAKER_FAILURES` / `RAG_BREAKER_RESET_S` | `3` / `60` | circuit breaker |
| `RAG_USER_RATE_LIMIT` | `10/10min` | per-user question limit |
| `RAG_HISTORY_TURNS` | `2` | turns of context sent with a question |
| `RAG_STORE_TRANSCRIPTS` | `True` | persist assistant Q&A in `support_messages` |
| `RAG_INGEST_SOURCES` | `repo-docs,faq,lab-descriptions` | which corpora `rag-ingest` sends |

Service-side settings (`RAG_MAX_CONCURRENCY`, `RAG_TOP_K`, `RAG_MIN_SCORE`,
`RAG_LLM_BACKEND`, `RAG_LOG_QUERIES`, …) live in the `hisdn-rag` container's environment,
documented in its own README — the dashboard neither reads nor needs them.

### CLI

| Command | Purpose | Suggested schedule |
| --- | --- | --- |
| `flask --app run.py cli rag-ingest` | build/refresh the assistant corpus | hourly |
| `flask --app run.py cli rag-ingest --dry-run` | show what would be ingested | on demand |
| `flask --app run.py cli rag-health` | check connectivity, index version, model | on demand / readiness |

---

## Testing

Everything on the dashboard side is testable **without a model**, by faking the client:

- **Mode routing** — a new thread with `mode=assistant` does not create staff work
  (`open_thread_count()` unchanged, `flush-support-emails` skips it); with `mode=support`
  the existing tests must keep passing untouched.
- **Assistant answer path** — mocked client returning `answered` persists an `assistant`
  message with `meta.sources`; `refused` persists the localized refusal; `unavailable`
  persists the localized fallback. In all three the user message survives.
- **Localization** — with `pt_BR` resolved, the refusal/fallback bodies come from the pt_BR
  catalog and the `locale` field reaches the client call.
- **Escalation** — `escalate_thread` flips the mode, writes the `system` message, makes the
  thread visible to `open_thread_count()`, and makes `flush-support-emails` e-mail it.
- **Escalation telemetry** — the `system` message's `meta` carries the page posted by the
  widget, the request's User-Agent and IP, and the counters (questions, refusals, negative
  votes); a thread with empty telemetry columns gets them backfilled; the rendered admin
  view and the mocked-mailer e-mail both contain the escalation page. A thread escalated
  from a *different* page than it started on keeps **both** values — the regression this
  guards against is the snapshot overwriting `origin_page`.
- **Feedback** — 👍 then 👎 on the same message leaves one `down` vote; the same vote twice
  clears it; a reason is stored only with `down`; another user's message returns 404; a
  `user`/`support`/`system` message rejects feedback; the aggregate query used by the admin
  panel counts what the fixtures set.
- **Circuit breaker** — N failures open it; `assistant_available()` goes false; the status
  endpoint reports it; it half-opens after the reset window.
- **Rate limit** — the N+1th question in the window is rejected without calling the client.
- **`RAG_STORE_TRANSCRIPTS=False`** — no assistant message is persisted, escalation carries
  only user messages.
- **Ingestion** — `rag-ingest --dry-run` collects the expected documents from a seeded DB
  and repo fixtures; `content_hash` is stable across runs; a deleted lab appears in the
  prune manifest.
- **Contract tests for `hisdn-rag`** (in its own repository/directory): retrieval gate
  refuses below threshold without generating; queue overflow returns `503`; the answer cache
  returns `cached: true` and is invalidated by an `index_version` bump; a pt-BR question
  against an en-only corpus retrieves and is answered in pt-BR; only cited chunks are
  returned as sources.

---

## Rollout

| Phase | Scope |
| --- | --- |
| 1 | `hisdn-rag` service + `/v1/answer` + `/v1/ingest` with `repo-docs` and `faq`; dashboard client, mode chooser, citations, **👍/👎 feedback**, escalation **with its telemetry snapshot**. `RAG_ENABLED=False` by default |
| 2 | Ingest `lab-descriptions` and (opt-in) `lab-guides`; admin stats panel (including the feedback report); cron ingestion |
| 3 | Enable for a pilot group (feature flag by user category/group), measure refusal rate, 👎 rate and latency, write FAQ entries for the top refusals |
| 4 | Enable for all users; keep the human path as the default for anything the assistant refuses |
| 5 | Optional: token streaming (SSE), accelerated backend, reranking |

---

## Open questions and risks

- **Latency expectations.** 10–20 s is a long time in a chat widget. Mitigations here are
  the typing indicator, the cache, and the FAQ corpus; if it still reads as too slow in the
  pilot, streaming (phase 5) is the fix that actually changes the perception.
- **Small models are worse at pt-BR than at English.** The pilot should measure answer
  quality per locale separately; if pt-BR quality lags, the answer is a bigger model on an
  accelerated host, not more prompt engineering.
- **Corpus coverage drives everything.** A RAG assistant over thin documentation refuses
  constantly. The refusal-rate metric is the feedback loop, and writing the FAQ corpus is a
  real part of the work, not a footnote.
- **Lab-guide confidentiality** is deployment-specific; hence opt-in ingestion rather than a
  guess.
- **Where does `hisdn-rag` live?** Same repository (a `rag/` directory, its own Dockerfile)
  keeps the contract and the client in sync; a separate repository matches its independent
  release cadence. Recommendation: start in-repo under `rag/`, split later if it grows.

---

## Future work

- Token **streaming** end-to-end (SSE `hisdn-rag` → dashboard → widget).
- **A replayable regression set** built from down-voted answers (question → expected
  sources), run against `hisdn-rag` after every model, prompt or chunking change. The data
  is collected from day one (see *Answer feedback*); this is the harness that consumes it.
- **Suggested follow-ups** derived from retrieved chunks.
- **Answering from resolved support cases** (staff-curated only, and only after review) —
  the highest-value corpus the deployment will ever have, and the one with the most privacy
  strings attached.
- **Third locale** — the design costs one catalog plus one system prompt.
