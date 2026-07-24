# -*- encoding: utf-8 -*-
"""Tests for the hisdn-rag service.

Everything here runs without model weights: the hashing embedder and a stub
generator stand in for the real backends, which is exactly the point of keeping
the pipeline free of hard dependencies.

Usage:
    pytest rag/tests -v
"""
import os
import sys

import pytest

RAG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAG_ROOT)

from ragsvc.cache import TTLCache, answer_key, normalize_question  # noqa: E402
from ragsvc.chunking import chunk_document, normalize_markdown, split_sections  # noqa: E402
from ragsvc.concurrency import GenerationGate, QueueFull  # noqa: E402
from ragsvc.config import Settings  # noqa: E402
from ragsvc.embeddings import HashingEmbedder  # noqa: E402
from ragsvc.generate import GenerationError, NullGenerator  # noqa: E402
from ragsvc.pipeline import Engine  # noqa: E402
from ragsvc.prompts import NO_ANSWER, build_messages, normalize_locale  # noqa: E402
from ragsvc.store import Store  # noqa: E402


DOC = """# Lab expiration

Lab instances expire automatically. Nothing here is about BGP.

## Extending a lab

To extend the expiration date of a running lab, open the lab instance page and
press the "Extend" button. An administrator can extend it further.

## Deleting a lab

Press "Delete" on the lab instance page. The lab is removed immediately.
"""


class StubGenerator:
    """Returns whatever the test queued, and records the prompts it saw."""

    name = "stub"
    extractive = False

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []
        self.raise_on_call = None

    def generate(self, messages, max_tokens=320, temperature=0.2, timeout_s=40):
        self.calls.append(messages)
        if self.raise_on_call:
            raise GenerationError(self.raise_on_call)
        reply = self.replies.pop(0) if self.replies else "an answer [1]"
        return reply, {"completion_tokens": len(reply.split())}


def build_engine(tmp_path, generator=None, **overrides):
    settings = Settings(
        data_dir=str(tmp_path),
        embed_backend="hashing",
        llm_backend="stub",
        min_score=overrides.pop("min_score", 0.2),
        **overrides,
    )
    return Engine(
        settings,
        store=Store(settings.db_path),
        embedder=HashingEmbedder(dim=256),
        generator=generator or StubGenerator(),
    )


def ingest_doc(engine, doc_id="doc/labs.md", text=DOC, lang="en", source="repo-docs", **kw):
    return engine.ingest(
        [
            {
                "doc_id": doc_id,
                "title": kw.get("title", "Labs"),
                "url": kw.get("url", f"/{doc_id}"),
                "lang": lang,
                "source": source,
                "content_hash": kw.get("content_hash", f"hash-of-{doc_id}-{len(text)}"),
                "text": text,
            }
        ]
    )


# --- chunking ---------------------------------------------------------------
class TestChunking:
    def test_headings_become_a_title_path(self):
        sections = split_sections(normalize_markdown(DOC), title="Developer guide")
        paths = [path for path, _body in sections]
        assert "Developer guide > Lab expiration > Extending a lab" in paths

    def test_hash_inside_a_fenced_block_is_not_a_heading(self):
        text = "# Title\n\n```bash\n# this is a shell comment\nls\n```\n\nbody\n"
        sections = split_sections(normalize_markdown(text), title="T")
        assert len(sections) == 1
        assert "shell comment" in sections[0][1]

    def test_chunks_carry_their_heading_path_and_respect_the_budget(self):
        chunks = chunk_document(DOC, title="Labs", chunk_tokens=40, overlap_tokens=10)
        assert len(chunks) > 1
        assert all(c.title_path.startswith("Labs") for c in chunks)
        assert [c.ord for c in chunks] == list(range(len(chunks)))

    def test_front_matter_and_html_comments_are_dropped(self):
        text = "---\ntitle: x\n---\n\n<!-- a comment -->\n# H\n\nbody\n"
        out = normalize_markdown(text)
        assert "title: x" not in out and "a comment" not in out and "body" in out


# --- cache ------------------------------------------------------------------
class TestCache:
    def test_normalization_folds_case_spacing_and_trailing_punctuation(self):
        assert normalize_question("  How  do I extend a Lab? ") == "how do i extend a lab"

    def test_key_changes_with_locale_and_index_version(self):
        base = answer_key("q", "en", 1)
        assert base != answer_key("q", "pt_BR", 1)
        assert base != answer_key("q", "en", 2)

    def test_ttl_expiry_and_lru_eviction(self):
        now = [1000.0]
        cache = TTLCache(max_entries=2, ttl_s=10, clock=lambda: now[0])
        cache.set("a", 1)
        assert cache.get("a") == 1
        now[0] += 11
        assert cache.get("a") is None
        cache.set("b", 2)
        cache.set("c", 3)
        cache.set("d", 4)
        assert cache.get("b") is None
        assert cache.get("d") == 4


# --- concurrency ------------------------------------------------------------
class TestGate:
    def test_overflow_is_rejected_rather_than_queued(self):
        gate = GenerationGate(max_concurrency=1, queue_max=0)
        with gate.slot():
            with pytest.raises(QueueFull):
                with gate.slot():
                    pass
        assert gate.stats()["rejected"] == 1
        # the slot is released again once the first request finishes
        with gate.slot():
            pass

    def test_capacity_counts_running_plus_queued(self):
        gate = GenerationGate(max_concurrency=2, queue_max=3)
        assert gate.capacity == 5


# --- store / retrieval ------------------------------------------------------
class TestStore:
    def test_upsert_replaces_chunks_and_prune_removes_missing_documents(self, tmp_path):
        engine = build_engine(tmp_path)
        ingest_doc(engine)
        first = engine.store.stats()
        assert first["documents"] == 1 and first["chunks"] > 0

        ingest_doc(engine, text=DOC + "\n\n## More\n\nextra section\n", content_hash="h2")
        assert engine.store.stats()["documents"] == 1

        engine.store.prune("repo-docs", keep_doc_ids=[])
        assert engine.store.stats()["documents"] == 0

    def test_unchanged_content_hash_is_skipped(self, tmp_path):
        engine = build_engine(tmp_path)
        first = ingest_doc(engine)
        again = ingest_doc(engine)
        assert first["indexed"] == 1
        assert again["indexed"] == 0 and again["skipped"] == 1
        assert again["index_version"] == first["index_version"]

    def test_changing_the_embedder_forces_a_full_reindex(self, tmp_path):
        engine = build_engine(tmp_path)
        ingest_doc(engine)
        engine.store.set_meta("embedder", "some-other-model")
        result = ingest_doc(engine)
        assert result["reembedded_all"] is True and result["indexed"] == 1

    def test_search_orders_by_similarity(self, tmp_path):
        engine = build_engine(tmp_path)
        ingest_doc(engine)
        hits = engine.store.search(engine.embedder.embed_query("extend the expiration date"), top_k=3)
        assert hits
        assert "Extend" in hits[0].text or "extend" in hits[0].text


# --- pipeline ---------------------------------------------------------------
class TestAnswerPipeline:
    def test_refuses_without_generating_when_nothing_is_relevant(self, tmp_path):
        generator = StubGenerator()
        engine = build_engine(tmp_path, generator=generator, min_score=0.9)
        ingest_doc(engine)
        result = engine.answer("what is the airspeed velocity of a swallow?", locale="en")
        assert result["status"] == "refused"
        assert result["reason"] == "no_relevant_context"
        assert result["sources"] == []
        assert generator.calls == []  # the generation slot was never spent

    def test_answers_with_only_the_cited_sources(self, tmp_path):
        generator = StubGenerator(replies=["Press the Extend button [1]."])
        engine = build_engine(tmp_path, generator=generator)
        ingest_doc(engine)
        result = engine.answer("how do I extend a lab?", locale="en")
        assert result["status"] == "answered"
        assert len(result["sources"]) == 1
        assert result["sources"][0]["index"] == 1
        assert result["sources"][0]["url"] == "/doc/labs.md"

    def test_sentinel_is_a_refusal(self, tmp_path):
        engine = build_engine(tmp_path, generator=StubGenerator(replies=[NO_ANSWER]))
        ingest_doc(engine)
        result = engine.answer("how do I extend a lab?", locale="en")
        assert result["status"] == "refused" and result["reason"] == "model_refused"

    def test_an_answer_citing_nothing_is_refused(self, tmp_path):
        engine = build_engine(
            tmp_path, generator=StubGenerator(replies=["Just press the shiny red button."])
        )
        ingest_doc(engine)
        result = engine.answer("how do I extend a lab?", locale="en")
        assert result["status"] == "refused" and result["reason"] == "not_grounded"

    def test_queue_overflow_reports_unavailable(self, tmp_path):
        engine = build_engine(tmp_path)
        ingest_doc(engine)
        engine.gate = GenerationGate(max_concurrency=1, queue_max=0)
        with engine.gate.slot():
            result = engine.answer("how do I extend a lab?", locale="en")
        assert result["status"] == "unavailable" and result["reason"] == "queue_full"

    def test_backend_failure_reports_unavailable(self, tmp_path):
        generator = StubGenerator()
        generator.raise_on_call = "model exploded"
        engine = build_engine(tmp_path, generator=generator)
        ingest_doc(engine)
        result = engine.answer("how do I extend a lab?", locale="en")
        assert result["status"] == "unavailable" and result["reason"] == "generation_failed"

    def test_repeated_question_is_served_from_cache(self, tmp_path):
        generator = StubGenerator(replies=["Press Extend [1].", "second call [1]"])
        engine = build_engine(tmp_path, generator=generator)
        ingest_doc(engine)
        first = engine.answer("How do I extend a lab?", locale="en")
        second = engine.answer("  how do i extend a LAB  ", locale="en")
        assert first["status"] == "answered" and first["cached"] is False
        assert second["cached"] is True
        assert second["answer"] == first["answer"]
        assert len(generator.calls) == 1

    def test_reingesting_invalidates_cached_answers(self, tmp_path):
        generator = StubGenerator(replies=["first [1]", "second [1]"])
        engine = build_engine(tmp_path, generator=generator)
        ingest_doc(engine)
        engine.answer("how do I extend a lab?", locale="en")
        ingest_doc(engine, text=DOC + "\n\nnew paragraph\n", content_hash="changed")
        result = engine.answer("how do I extend a lab?", locale="en")
        assert result["cached"] is False
        assert len(generator.calls) == 2

    def test_locale_selects_the_prompt_and_is_cached_separately(self, tmp_path):
        generator = StubGenerator(replies=["Pressione Estender [1].", "Press Extend [1]."])
        # The hashing embedder is lexical, so a pt-BR question does not match the
        # English corpus; the retrieval gate is exercised elsewhere -- here we
        # only care that the locale picks the prompt and splits the cache.
        engine = build_engine(tmp_path, generator=generator, min_score=-1.0)
        ingest_doc(engine)
        pt = engine.answer("como estender um lab?", locale="pt_BR")
        en = engine.answer("como estender um lab?", locale="en")
        assert pt["status"] == "answered" and en["status"] == "answered"
        assert len(generator.calls) == 2
        assert "português do Brasil" in generator.calls[0][0]["content"]
        assert "English" in generator.calls[1][0]["content"]

    def test_pt_br_question_retrieves_from_an_english_corpus(self, tmp_path):
        # The hashing embedder cannot do this (it is lexical), so assert the
        # mechanism instead: an en chunk is still reachable and a same-language
        # chunk gets the configured bonus.
        engine = build_engine(tmp_path, same_lang_bonus=0.5)
        ingest_doc(engine, doc_id="en.md", lang="en")
        ingest_doc(engine, doc_id="pt.md", lang="pt_BR")
        query = engine.embedder.embed_query("extend the expiration date")
        hits = engine.store.search(query, top_k=4, lang="pt_BR", same_lang_bonus=0.5)
        assert hits[0].lang == "pt_BR"
        assert any(h.lang == "en" for h in hits)

    def test_history_is_forwarded_to_the_model(self, tmp_path):
        generator = StubGenerator(replies=["ok [1]"])
        engine = build_engine(tmp_path, generator=generator, min_score=-1.0)
        ingest_doc(engine)
        engine.answer(
            "and how do I undo that?",
            locale="en",
            history=[{"role": "user", "content": "how do I extend a lab?"}],
        )
        roles = [m["role"] for m in generator.calls[0]]
        assert roles == ["system", "user", "user"]

    def test_retrieval_only_backend_returns_the_best_chunk(self, tmp_path):
        engine = build_engine(tmp_path, generator=NullGenerator())
        ingest_doc(engine)
        result = engine.answer("how do I extend a lab?", locale="en")
        assert result["status"] == "answered"
        assert result["sources"] and result["usage"]["completion_tokens"] == 0

    def test_empty_question_is_refused(self, tmp_path):
        engine = build_engine(tmp_path)
        assert engine.answer("   ")["status"] == "refused"

    def test_stats_report_corpus_and_counters(self, tmp_path):
        engine = build_engine(tmp_path, generator=StubGenerator(replies=["a [1]"]))
        ingest_doc(engine)
        engine.answer("how do I extend a lab?", locale="en")
        stats = engine.stats()
        assert stats["corpus"]["documents"] == 1
        assert stats["answers"]["answered"] == 1
        assert stats["gate"]["max_concurrency"] == 1


# --- prompts ----------------------------------------------------------------
class TestPrompts:
    def test_unknown_locales_fall_back_to_english(self):
        assert normalize_locale("pt-BR") == "pt_BR"
        assert normalize_locale("pt") == "pt_BR"
        assert normalize_locale("fr_CA") == "en"
        assert normalize_locale(None) == "en"

    def test_context_blocks_are_numbered_for_citation(self, tmp_path):
        engine = build_engine(tmp_path)
        ingest_doc(engine)
        hits = engine.store.search(engine.embedder.embed_query("extend"), top_k=2)
        messages = build_messages("q?", hits, locale="en")
        assert "[1]" in messages[-1]["content"]
        assert NO_ANSWER in messages[0]["content"]


# --- HTTP layer (skipped when FastAPI is not installed) ---------------------
class TestApi:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from ragsvc import api, config

        monkeypatch.setenv("RAG_SERVICE_TOKEN", "s3cret")
        monkeypatch.setenv("RAG_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("RAG_EMBED_BACKEND", "hashing")
        config.reset_settings()
        engine = build_engine(tmp_path, generator=StubGenerator(replies=["Press Extend [1]."]))
        ingest_doc(engine)
        api.set_engine(engine)
        yield TestClient(api.app)
        api.set_engine(None)
        config.reset_settings()

    def test_answer_requires_the_bearer_token(self, client):
        assert client.post("/v1/answer", json={"question": "q"}).status_code == 401

    def test_answer_returns_sources(self, client):
        response = client.post(
            "/v1/answer",
            json={"question": "how do I extend a lab?", "locale": "en"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "answered"
        assert response.json()["sources"]

    def test_healthz_needs_no_token(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200 and response.json()["ready"] is True
