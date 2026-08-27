# -*- encoding: utf-8 -*-
"""Embedding backends.

Two backends:

* ``sentence_transformers`` -- the real one. A small multilingual model so a
  pt-BR question retrieves English documentation (the common case for this
  corpus). Loaded lazily and strictly from a local path: the container runs
  offline, weights are baked in or mounted.
* ``hashing`` -- a deterministic, dependency-free bag-of-features embedder used
  by the tests and by ``--dry-run`` style local experiments. It retrieves badly
  and must never be used to serve users; it exists so the whole pipeline can be
  exercised without downloading 500 MB of weights.

Vectors are L2-normalized on the way out, so cosine similarity is a dot product.
"""
import hashlib
import math
import re
import unicodedata

WORD_RE = re.compile(r"\w+", re.UNICODE)


def normalize(vec):
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return list(vec)
    return [v / norm for v in vec]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _tokens(text):
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return WORD_RE.findall(text)


class HashingEmbedder:
    """Deterministic hashing embedder -- dev/test only, no external weights."""

    def __init__(self, dim=384):
        self.dim = dim
        self.name = f"hashing-{dim}"

    def _embed_one(self, text):
        vec = [0.0] * self.dim
        words = _tokens(text)
        # unigrams plus bigrams, so word order carries a little signal
        features = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
        for feat in features:
            digest = hashlib.sha1(feat.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign
        return normalize(vec)

    def embed_documents(self, texts):
        return [self._embed_one(t) for t in texts]

    def embed_query(self, text):
        return self._embed_one(text)


class SentenceTransformerEmbedder:
    """Local sentence-transformers model (multilingual, small, CPU)."""

    def __init__(self, model_path, dim=384, query_prefix="", passage_prefix=""):
        if not model_path:
            raise RuntimeError(
                "RAG_EMBED_MODEL_PATH is required for the sentence_transformers backend"
            )
        self.model_path = model_path
        self.dim = dim
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self.name = model_path.rstrip("/").rsplit("/", 1)[-1]
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # lazy: heavy import

            self._model = SentenceTransformer(self.model_path, device="cpu")
            self.dim = self._model.get_sentence_embedding_dimension()
        return self._model

    def _encode(self, texts):
        vectors = self.model.encode(
            texts, batch_size=16, show_progress_bar=False, normalize_embeddings=True
        )
        return [list(map(float, v)) for v in vectors]

    def embed_documents(self, texts):
        return self._encode([self.passage_prefix + t for t in texts])

    def embed_query(self, text):
        return self._encode([self.query_prefix + text])[0]


def build_embedder(settings):
    backend = (settings.embed_backend or "").lower()
    if backend == "hashing":
        return HashingEmbedder(dim=settings.embed_dim)
    if backend == "sentence_transformers":
        return SentenceTransformerEmbedder(
            settings.embed_model_path,
            dim=settings.embed_dim,
            query_prefix=settings.embed_query_prefix,
            passage_prefix=settings.embed_passage_prefix,
        )
    raise RuntimeError(f"unknown RAG_EMBED_BACKEND: {settings.embed_backend!r}")
