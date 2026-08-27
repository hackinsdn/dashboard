# -*- encoding: utf-8 -*-
"""Chunk store and vector index, backed by a single SQLite file.

The corpus is small (order of 10^3 chunks), so an exact flat search over
in-memory vectors is both faster and far simpler than an approximate index --
and it never returns a different answer than the brute-force truth. numpy is
used when available; the pure-Python path is a few milliseconds slower at this
size and keeps the module importable anywhere.

Writes happen in one transaction per ingestion run and end with an index-version
bump, which is what invalidates the answer cache: readers never observe a
half-written corpus.
"""
import json
import os
import sqlite3
import threading
import time
from array import array
from dataclasses import dataclass

try:  # optional: only a speedup
    import numpy as _np
except ImportError:  # pragma: no cover - exercised by not having numpy
    _np = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    title        TEXT,
    url          TEXT,
    lang         TEXT,
    content_hash TEXT,
    updated_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ord        INTEGER NOT NULL,
    title_path TEXT,
    text       TEXT NOT NULL,
    lang       TEXT,
    embedding  BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class Hit:
    chunk_id: int
    doc_id: str
    title: str
    title_path: str
    url: str
    lang: str
    source: str
    text: str
    score: float


def pack(vec):
    return array("f", vec).tobytes()


def unpack(blob):
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


class Store:
    def __init__(self, db_path):
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._vectors = None  # (rows, matrix) cache
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")

    # --- connections -----------------------------------------------------
    def connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    # --- meta ------------------------------------------------------------
    def get_meta(self, key, default=None):
        row = self.connect().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        conn = self.connect()
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()

    @property
    def index_version(self):
        return int(self.get_meta("index_version", "0") or 0)

    def bump_index_version(self):
        version = self.index_version + 1
        self.set_meta("index_version", version)
        self.set_meta("index_built_at", time.time())
        self._vectors = None
        return version

    # --- ingestion -------------------------------------------------------
    def content_hash_of(self, doc_id):
        row = self.connect().execute(
            "SELECT content_hash FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        return row["content_hash"] if row else None

    def upsert_document(self, doc, chunks, vectors):
        """Replace a document and all of its chunks.

        ``chunks`` are chunking.Chunk instances, ``vectors`` their embeddings in
        the same order. Replacing wholesale (rather than diffing chunks) keeps
        ordinals contiguous and is trivially correct; documents are small.
        """
        conn = self.connect()
        with self._lock:
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc["doc_id"],))
            conn.execute(
                "INSERT INTO documents(doc_id, source, title, url, lang, content_hash, updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET "
                "source=excluded.source, title=excluded.title, url=excluded.url, "
                "lang=excluded.lang, content_hash=excluded.content_hash, updated_at=excluded.updated_at",
                (
                    doc["doc_id"],
                    doc.get("source") or "",
                    doc.get("title") or "",
                    doc.get("url") or "",
                    doc.get("lang") or "",
                    doc.get("content_hash") or "",
                    time.time(),
                ),
            )
            conn.executemany(
                "INSERT INTO chunks(doc_id, ord, title_path, text, lang, embedding) VALUES(?,?,?,?,?,?)",
                [
                    (
                        doc["doc_id"],
                        chunk.ord,
                        chunk.title_path,
                        chunk.text,
                        doc.get("lang") or "",
                        pack(vec),
                    )
                    for chunk, vec in zip(chunks, vectors)
                ],
            )
            conn.commit()
            self._vectors = None

    def prune(self, source, keep_doc_ids):
        """Delete documents of ``source`` that are no longer in the corpus."""
        conn = self.connect()
        with self._lock:
            rows = conn.execute(
                "SELECT doc_id FROM documents WHERE source=?", (source,)
            ).fetchall()
            keep = set(keep_doc_ids or [])
            stale = [r["doc_id"] for r in rows if r["doc_id"] not in keep]
            for doc_id in stale:
                conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
                conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
            conn.commit()
            if stale:
                self._vectors = None
            return len(stale)

    # --- retrieval -------------------------------------------------------
    def _load_vectors(self):
        if self._vectors is not None:
            return self._vectors
        rows = self.connect().execute(
            "SELECT c.id, c.doc_id, c.ord, c.title_path, c.text, c.lang, c.embedding, "
            "       d.title, d.url, d.source "
            "FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
            "ORDER BY c.id"
        ).fetchall()
        meta = [
            {
                "id": r["id"],
                "doc_id": r["doc_id"],
                "title_path": r["title_path"] or "",
                "text": r["text"],
                "lang": r["lang"] or "",
                "title": r["title"] or "",
                "url": r["url"] or "",
                "source": r["source"] or "",
            }
            for r in rows
        ]
        vectors = [unpack(r["embedding"]) for r in rows]
        if _np is not None and vectors:
            vectors = _np.array(vectors, dtype="float32")
        self._vectors = (meta, vectors)
        return self._vectors

    def search(self, query_vec, top_k=4, lang=None, same_lang_bonus=0.0):
        """Exact cosine search (vectors are stored L2-normalized)."""
        meta, vectors = self._load_vectors()
        if not meta:
            return []
        if _np is not None and not isinstance(vectors, list):
            scores = vectors.dot(_np.array(query_vec, dtype="float32")).tolist()
        else:
            scores = [sum(a * b for a, b in zip(vec, query_vec)) for vec in vectors]

        scored = []
        for item, score in zip(meta, scores):
            if lang and same_lang_bonus and item["lang"] == lang:
                score += same_lang_bonus
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        hits = []
        for score, item in scored[:top_k]:
            hits.append(
                Hit(
                    chunk_id=item["id"],
                    doc_id=item["doc_id"],
                    title=item["title"],
                    title_path=item["title_path"],
                    url=item["url"],
                    lang=item["lang"],
                    source=item["source"],
                    text=item["text"],
                    score=float(score),
                )
            )
        return hits

    # --- introspection ---------------------------------------------------
    def stats(self):
        conn = self.connect()
        docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        by_source = {
            r["source"]: r["n"]
            for r in conn.execute(
                "SELECT source, COUNT(*) AS n FROM documents GROUP BY source"
            ).fetchall()
        }
        built_at = self.get_meta("index_built_at")
        return {
            "documents": docs,
            "chunks": chunks,
            "documents_by_source": by_source,
            "index_version": self.index_version,
            "index_built_at": float(built_at) if built_at else None,
            "embedder": self.get_meta("embedder"),
            "vector_backend": "numpy" if _np is not None else "python",
        }

    def dump_meta(self):
        return json.dumps(self.stats(), sort_keys=True)
