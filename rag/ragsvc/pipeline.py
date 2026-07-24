# -*- encoding: utf-8 -*-
"""The answer pipeline: retrieve, gate, generate, verify.

Refusal is the design's first-class outcome, not an error path. There are two
gates on the way to an answer:

* **before generation** -- if the best retrieved chunk scores below
  ``RAG_MIN_SCORE`` the service refuses without spending a generation slot.
  This is both the correctness mechanism for off-topic questions and, on a
  CPU-only host, the cheapest possible answer.
* **after generation** -- the model's ``NO_ANSWER`` sentinel is not trusted on
  its own: an answer that cites nothing, or that is wildly longer than the
  context it was given, is a hallucination tell and is refused too.

Only blocks the answer actually cited come back as sources.
"""
import logging
import re
import time

from . import prompts
from .cache import TTLCache, answer_key, question_hash
from .chunking import chunk_document, estimate_tokens
from .concurrency import GenerationGate, QueueFull
from .embeddings import build_embedder
from .generate import GenerationError, build_generator
from .store import Store

log = logging.getLogger("ragsvc")

CITATION_RE = re.compile(r"\[(\d{1,2})\]")
# the sentinel, plus the shapes small models produce when they paraphrase it
NO_ANSWER_RE = re.compile(r"\bNO[_\s-]?ANSWER\b", re.IGNORECASE)


class Engine:
    def __init__(self, settings, store=None, embedder=None, generator=None, gate=None):
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.embedder = embedder or build_embedder(settings)
        self.generator = generator or build_generator(settings)
        self.gate = gate or GenerationGate(
            max_concurrency=settings.max_concurrency, queue_max=settings.queue_max
        )
        self.answer_cache = TTLCache(
            max_entries=settings.cache_max_entries, ttl_s=settings.cache_ttl_s
        )
        self.embedding_cache = TTLCache(
            max_entries=settings.cache_max_entries, ttl_s=settings.cache_ttl_s
        )
        self.counters = {
            "answered": 0,
            "refused": 0,
            "unavailable": 0,
            "latency_ms": [],  # bounded below
        }

    # --- helpers ---------------------------------------------------------
    def _embed_query(self, question):
        key = question_hash(question)
        cached = self.embedding_cache.get(key)
        if cached is not None:
            return cached
        vector = self.embedder.embed_query(question)
        self.embedding_cache.set(key, vector)
        return vector

    def _trim_context(self, hits, question, locale):
        """Drop the lowest-scoring chunks until the prompt fits the budget."""
        overhead = estimate_tokens(prompts.system_prompt(locale)) + estimate_tokens(question) + 32
        budget = max(200, self.settings.max_prompt_tokens - overhead)
        kept, used = [], 0
        for hit in hits:  # already ordered best-first
            cost = estimate_tokens(hit.text) + estimate_tokens(hit.title_path) + 8
            if kept and used + cost > budget:
                continue
            kept.append(hit)
            used += cost
        return kept, used

    def _sources_from(self, answer, hits):
        """Return only the blocks the answer actually cited, best-scoring first."""
        cited = {int(n) for n in CITATION_RE.findall(answer or "")}
        chosen, seen_docs = [], set()
        for index, hit in enumerate(hits, start=1):
            if index not in cited or hit.doc_id in seen_docs:
                continue
            seen_docs.add(hit.doc_id)
            chosen.append(
                {
                    "index": index,
                    "doc_id": hit.doc_id,
                    "title": hit.title_path or hit.title or hit.doc_id,
                    "url": hit.url,
                    "lang": hit.lang,
                    "source": hit.source,
                    "score": round(hit.score, 4),
                    "snippet": hit.text[:280],
                }
            )
        chosen.sort(key=lambda s: s["score"], reverse=True)
        return chosen[: self.settings.max_citations]

    def _log_query(self, question, result, hits):
        mode = (self.settings.log_queries or "none").lower()
        if mode == "none":
            return
        payload = {
            "status": result.get("status"),
            "qhash": question_hash(question),
            "docs": [h.doc_id for h in hits],
            "latency_ms": result.get("usage", {}).get("latency_ms"),
        }
        if mode == "full":
            payload["question"] = question
            payload["answer"] = result.get("answer")
        log.info("query %s", payload)

    def _record(self, status, latency_ms):
        self.counters[status] = self.counters.get(status, 0) + 1
        samples = self.counters["latency_ms"]
        samples.append(latency_ms)
        if len(samples) > 200:
            del samples[:-200]

    # --- public API ------------------------------------------------------
    def answer(self, question, locale="en", history=None, conversation_id=None, top_k=None):
        started = time.monotonic()
        question = (question or "").strip()
        if not question:
            return {"status": "refused", "reason": "empty_question", "sources": []}

        locale = prompts.normalize_locale(locale)
        key = answer_key(question, locale, self.store.index_version)
        cached = self.answer_cache.get(key)
        if cached is not None:
            result = dict(cached, cached=True)
            self._record(result["status"], 0)
            return result

        top_k = top_k or self.settings.top_k
        query_vec = self._embed_query(question)
        hits = self.store.search(
            query_vec,
            top_k=top_k,
            lang=locale,
            same_lang_bonus=self.settings.same_lang_bonus,
        )

        # Gate 1: nothing relevant -> refuse without spending a generation slot.
        if not hits or hits[0].score < self.settings.min_score:
            result = {
                "status": "refused",
                "reason": "no_relevant_context",
                "sources": [],
                "cached": False,
                "usage": {"latency_ms": int((time.monotonic() - started) * 1000)},
            }
            self.answer_cache.set(key, dict(result, cached=False))
            self._record("refused", result["usage"]["latency_ms"])
            self._log_query(question, result, hits)
            return result

        kept, context_tokens = self._trim_context(hits, question, locale)

        # Retrieval-only mode: hand back the best chunk instead of prose.
        if getattr(self.generator, "extractive", False):
            best = kept[0]
            result = {
                "status": "answered",
                "answer": f"{best.text}\n\n[1]",
                "sources": self._sources_from("[1]", kept),
                "cached": False,
                "usage": {
                    "prompt_tokens": context_tokens,
                    "completion_tokens": 0,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                },
            }
            self.answer_cache.set(key, dict(result, cached=False))
            self._record("answered", result["usage"]["latency_ms"])
            return result

        messages = prompts.build_messages(question, kept, locale=locale, history=history)

        try:
            with self.gate.slot():
                text, usage = self.generator.generate(
                    messages,
                    max_tokens=self.settings.max_output_tokens,
                    temperature=self.settings.temperature,
                    timeout_s=self.settings.gen_timeout_s,
                )
        except QueueFull as exc:
            log.warning("rejecting request: %s", exc)
            latency = int((time.monotonic() - started) * 1000)
            self._record("unavailable", latency)
            return {
                "status": "unavailable",
                "reason": "queue_full",
                "retry_after_s": 20,
                "sources": [],
            }
        except GenerationError as exc:
            log.error("generation failed: %s", exc)
            latency = int((time.monotonic() - started) * 1000)
            self._record("unavailable", latency)
            return {
                "status": "unavailable",
                "reason": "generation_failed",
                "retry_after_s": 5,
                "sources": [],
            }

        latency = int((time.monotonic() - started) * 1000)
        sources = self._sources_from(text, kept)

        # Gate 2: the sentinel is not trusted alone -- an answer that cites
        # nothing, or that dwarfs the context it was built from, is refused.
        refused_reason = None
        if not text or NO_ANSWER_RE.search(text):
            refused_reason = "model_refused"
        elif not sources:
            refused_reason = "not_grounded"
        elif estimate_tokens(text) > 2 * max(1, context_tokens):
            refused_reason = "answer_exceeds_context"

        if refused_reason:
            result = {
                "status": "refused",
                "reason": refused_reason,
                "sources": [],
                "cached": False,
                "usage": {"latency_ms": latency, "prompt_tokens": context_tokens},
            }
        else:
            result = {
                "status": "answered",
                "answer": CITATION_RE.sub(lambda m: f"[{m.group(1)}]", text).strip(),
                "sources": sources,
                "cached": False,
                "usage": {
                    "prompt_tokens": context_tokens,
                    "completion_tokens": usage.get("completion_tokens"),
                    "truncated": usage.get("truncated", False),
                    "latency_ms": latency,
                },
            }

        self.answer_cache.set(key, dict(result, cached=False))
        self._record(result["status"], latency)
        self._log_query(question, result, kept)
        return result

    def ingest(self, documents, prune=None):
        """Upsert documents; skip the ones whose content hash is unchanged."""
        settings = self.settings
        embedder_name = getattr(self.embedder, "name", settings.embed_backend)
        stored_embedder = self.store.get_meta("embedder")
        # A different embedding model makes every stored vector meaningless:
        # re-embed everything, ignoring content hashes.
        force = stored_embedder is not None and stored_embedder != embedder_name

        indexed = skipped = chunk_count = 0
        for doc in documents or []:
            doc_id = doc.get("doc_id")
            if not doc_id:
                continue
            content_hash = doc.get("content_hash") or ""
            if not force and content_hash and self.store.content_hash_of(doc_id) == content_hash:
                skipped += 1
                continue
            chunks = chunk_document(
                doc.get("text") or "",
                title=doc.get("title") or "",
                chunk_tokens=settings.chunk_tokens,
                overlap_tokens=settings.chunk_overlap_tokens,
            )
            if not chunks:
                skipped += 1
                continue
            vectors = self.embedder.embed_documents([c.text for c in chunks])
            self.store.upsert_document(doc, chunks, vectors)
            indexed += 1
            chunk_count += len(chunks)

        pruned = 0
        if prune and prune.get("source"):
            pruned = self.store.prune(prune["source"], prune.get("keep_doc_ids") or [])

        if indexed or pruned:
            self.store.set_meta("embedder", embedder_name)
            version = self.store.bump_index_version()
            # Cached answers cite the old corpus; the key includes the version,
            # so this is belt-and-braces rather than strictly required.
            self.answer_cache.clear()
        else:
            version = self.store.index_version
            if stored_embedder is None:
                self.store.set_meta("embedder", embedder_name)

        return {
            "indexed": indexed,
            "skipped": skipped,
            "chunks": chunk_count,
            "pruned": pruned,
            "reembedded_all": force,
            "index_version": version,
        }

    def stats(self):
        samples = sorted(self.counters["latency_ms"])
        p50 = samples[len(samples) // 2] if samples else None
        p95 = samples[int(len(samples) * 0.95)] if samples else None
        return {
            "corpus": self.store.stats(),
            "answers": {
                "answered": self.counters.get("answered", 0),
                "refused": self.counters.get("refused", 0),
                "unavailable": self.counters.get("unavailable", 0),
                "latency_p50_ms": p50,
                "latency_p95_ms": p95,
            },
            "answer_cache": self.answer_cache.stats(),
            "embedding_cache": self.embedding_cache.stats(),
            "gate": self.gate.stats(),
            "backends": {
                "embeddings": getattr(self.embedder, "name", self.settings.embed_backend),
                "generation": getattr(self.generator, "name", self.settings.llm_backend),
                "llm_backend": self.settings.llm_backend,
            },
            "limits": {
                "top_k": self.settings.top_k,
                "min_score": self.settings.min_score,
                "max_prompt_tokens": self.settings.max_prompt_tokens,
                "max_output_tokens": self.settings.max_output_tokens,
            },
            "log_queries": self.settings.log_queries,
        }

    def health(self):
        corpus = self.store.stats()
        ready = corpus["chunks"] > 0
        return {
            "status": "ok" if ready else "degraded",
            "ready": ready,
            "index_version": corpus["index_version"],
            "chunks": corpus["chunks"],
            "llm_backend": self.settings.llm_backend,
        }
