# -*- encoding: utf-8 -*-
"""Configuration for the hisdn-rag service.

Every knob is an environment variable so the same image serves the default
CPU-constrained profile and an accelerated (GPU) deployment -- see
doc/rag-assistant-design.md. Defaults here are the CPU profile.
"""
import os
from dataclasses import dataclass, field


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name, default=False):
    return os.getenv(name, "True" if default else "False") == "True"


@dataclass
class Settings:
    # --- storage ---
    data_dir: str = field(default_factory=lambda: os.getenv("RAG_DATA_DIR", "/data"))

    # --- auth ---
    service_token: str = field(default_factory=lambda: os.getenv("RAG_SERVICE_TOKEN", ""))

    # --- retrieval ---
    top_k: int = field(default_factory=lambda: _int("RAG_TOP_K", 4))
    min_score: float = field(default_factory=lambda: _float("RAG_MIN_SCORE", 0.35))
    same_lang_bonus: float = field(default_factory=lambda: _float("RAG_SAME_LANG_BONUS", 0.05))
    max_citations: int = field(default_factory=lambda: _int("RAG_MAX_CITATIONS", 3))

    # --- chunking ---
    chunk_tokens: int = field(default_factory=lambda: _int("RAG_CHUNK_TOKENS", 400))
    chunk_overlap_tokens: int = field(default_factory=lambda: _int("RAG_CHUNK_OVERLAP_TOKENS", 60))

    # --- prompt / generation budget ---
    max_prompt_tokens: int = field(default_factory=lambda: _int("RAG_MAX_PROMPT_TOKENS", 1600))
    max_output_tokens: int = field(default_factory=lambda: _int("RAG_MAX_OUTPUT_TOKENS", 320))
    temperature: float = field(default_factory=lambda: _float("RAG_TEMPERATURE", 0.2))
    gen_timeout_s: float = field(default_factory=lambda: _float("RAG_GEN_TIMEOUT_S", 40))
    n_ctx: int = field(default_factory=lambda: _int("RAG_N_CTX", 4096))
    n_threads: int = field(default_factory=lambda: _int("RAG_N_THREADS", 0))  # 0 = library default

    # --- concurrency ---
    max_concurrency: int = field(default_factory=lambda: _int("RAG_MAX_CONCURRENCY", 1))
    queue_max: int = field(default_factory=lambda: _int("RAG_QUEUE_MAX", 4))
    # How long a queued request waits for the slot before it gives up and frees
    # its place. MUST be bounded: an unbounded wait lets requests whose client
    # already timed out pile up and pin the queue full forever. Keep it below the
    # dashboard's RAG_TIMEOUT_S so a queued request that will not be served in
    # time evicts itself rather than being abandoned while still holding a slot.
    queue_wait_s: float = field(default_factory=lambda: _float("RAG_QUEUE_WAIT_S", 30))

    # --- cache ---
    cache_ttl_s: int = field(default_factory=lambda: _int("RAG_CACHE_TTL_S", 86400))
    cache_max_entries: int = field(default_factory=lambda: _int("RAG_CACHE_MAX_ENTRIES", 512))

    # --- backends ---
    # embeddings: sentence_transformers | hashing (dev/tests only)
    embed_backend: str = field(default_factory=lambda: os.getenv("RAG_EMBED_BACKEND", "sentence_transformers"))
    embed_model_path: str = field(default_factory=lambda: os.getenv("RAG_EMBED_MODEL_PATH", ""))
    embed_dim: int = field(default_factory=lambda: _int("RAG_EMBED_DIM", 384))
    # the query/passage prefixes E5-family models are trained with; harmless for others
    embed_query_prefix: str = field(default_factory=lambda: os.getenv("RAG_EMBED_QUERY_PREFIX", "query: "))
    embed_passage_prefix: str = field(default_factory=lambda: os.getenv("RAG_EMBED_PASSAGE_PREFIX", "passage: "))

    # generation: llamacpp | openai_compat | none
    llm_backend: str = field(default_factory=lambda: os.getenv("RAG_LLM_BACKEND", "llamacpp"))
    llm_model_path: str = field(default_factory=lambda: os.getenv("RAG_LLM_MODEL_PATH", ""))
    llm_base_url: str = field(default_factory=lambda: os.getenv("RAG_LLM_BASE_URL", ""))
    llm_model_name: str = field(default_factory=lambda: os.getenv("RAG_LLM_MODEL_NAME", "local"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("RAG_LLM_API_KEY", ""))

    # --- privacy / logging ---
    # none | hashed | full  (see doc/rag-assistant-design.md, "Privacy and logging controls")
    log_queries: str = field(default_factory=lambda: os.getenv("RAG_LOG_QUERIES", "none"))

    # --- ingestion ---
    ingest_batch: int = field(default_factory=lambda: _int("RAG_INGEST_BATCH", 32))

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "index.sqlite3")


_settings = None


def get_settings():
    """Process-wide settings (env is read once)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings():
    """Test hook: force the next get_settings() to re-read the environment."""
    global _settings
    _settings = None
