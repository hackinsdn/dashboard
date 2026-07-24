# -*- encoding: utf-8 -*-
"""Build the RAG assistant's corpus.

Ingestion is driven from here (the dashboard owns the database and the repo)
and executed by the hisdn-rag service (it owns the embedding model). Documents
are posted with a ``content_hash``, so an unchanged document costs a comparison
rather than an embedding, and each source ends with a ``prune`` manifest so a
run converges the index to the current corpus instead of only ever growing it.

Only public-facing product documentation is ingested: no user data, no support
transcripts, no manifests. See doc/rag-assistant-design.md ("Local document
ingestion").

Invoked from cron via the ``rag-ingest`` CLI command (see apps/cli/routes.py).
"""
import hashlib
import os

from apps.controllers import rag_client
from apps.home.models import Labs

# Repository documents that are useful to a *user* of the dashboard. The
# developer/testing plans under doc/ are deliberately excluded: nobody asks the
# support chat about a test-coverage plan, and every extra document dilutes
# retrieval.
REPO_DOCS = [
    "README.md",
    "doc/GETTING_STARTED.md",
    "doc/INSTALL.md",
    "doc/DEV.md",
    "doc/i18n.md",
    "doc/fork-lab.md",
    "doc/MAP_CONFIGURATION.md",
    "doc/UI-Customizations.md",
    "doc/release-notes.md",
]

FAQ_DIR = "doc/faq"


def _hash(text):
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _title_of(text, fallback):
    for line in (text or "").split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def collect_repo_docs(app):
    """Markdown documentation shipped with the repository."""
    root = _repo_root()
    base_url = app.config.get("RAG_DOCS_BASE_URL", "").rstrip("/")
    documents = []
    for rel in REPO_DOCS:
        text = _read(os.path.join(root, rel))
        if not text:
            continue
        documents.append(
            {
                "doc_id": rel,
                "title": _title_of(text, rel),
                "url": f"{base_url}/{rel}" if base_url else rel,
                "lang": "en",
                "source": "repo-docs",
                "content_hash": _hash(text),
                "text": text,
            }
        )
    return documents


def collect_faq(app):
    """Curated FAQ written for the assistant, one file per language."""
    root = _repo_root()
    faq_dir = os.path.join(root, FAQ_DIR)
    if not os.path.isdir(faq_dir):
        return []
    documents = []
    for name in sorted(os.listdir(faq_dir)):
        if not name.endswith(".md"):
            continue
        text = _read(os.path.join(faq_dir, name))
        if not text:
            continue
        # faq.pt_BR.md -> pt_BR ; faq.en.md -> en
        parts = name[:-3].split(".")
        lang = parts[1] if len(parts) > 1 else "en"
        documents.append(
            {
                "doc_id": f"{FAQ_DIR}/{name}",
                "title": _title_of(text, name),
                "url": f"{app.config.get('BASE_URL', '').rstrip('/')}/",
                "lang": lang,
                "source": "faq",
                "content_hash": _hash(text),
                "text": text,
            }
        )
    return documents


def collect_lab_descriptions(app):
    """What each lab is about -- title, description, goals, categories."""
    base_url = app.config.get("BASE_URL", "").rstrip("/")
    documents = []
    for lab in Labs.query.filter_by(is_deleted=False).all():
        extended = lab.extended_desc.decode("utf-8", "replace") if lab.extended_desc else ""
        categories = ", ".join(c.category for c in (lab.categories or []))
        parts = [f"# {lab.title or lab.id}"]
        if categories:
            parts.append(f"Categories: {categories}")
        if lab.description:
            parts.append(lab.description)
        if lab.goals:
            parts.append(f"## Goals\n\n{lab.goals}")
        if extended:
            parts.append(f"## Details\n\n{extended}")
        text = "\n\n".join(parts)
        documents.append(
            {
                "doc_id": f"lab:{lab.id}",
                "title": lab.title or lab.id,
                "url": f"{base_url}/labs/{lab.id}",
                "lang": "",  # lab content is authored in either language
                "source": "lab-descriptions",
                "content_hash": _hash(text),
                "text": text,
            }
        )
    return documents


def collect_lab_guides(app):
    """Full lab guides. Opt-in: some deployments treat these as restricted."""
    base_url = app.config.get("BASE_URL", "").rstrip("/")
    documents = []
    for lab in Labs.query.filter_by(is_deleted=False).all():
        if not lab.lab_guide_md:
            continue
        text = lab.lab_guide_md.decode("utf-8", "replace")
        documents.append(
            {
                "doc_id": f"lab-guide:{lab.id}",
                "title": f"{lab.title or lab.id} (lab guide)",
                "url": f"{base_url}/labs/{lab.id}",
                "lang": "",
                "source": "lab-guides",
                "content_hash": _hash(text),
                "text": text,
            }
        )
    return documents


COLLECTORS = {
    "repo-docs": collect_repo_docs,
    "faq": collect_faq,
    "lab-descriptions": collect_lab_descriptions,
    "lab-guides": collect_lab_guides,
}


def run_ingest(app, sources=None, dry_run=False, full=False):
    """Collect the enabled corpora and push them to the assistant service."""
    if not app.config.get("RAG_ENABLED") and not dry_run:
        app.logger.info("rag-ingest: RAG_ENABLED is off, nothing to do")
        return None

    enabled = sources or app.config.get("RAG_INGEST_SOURCES") or []
    unknown = [s for s in enabled if s not in COLLECTORS]
    if unknown:
        app.logger.error(f"rag-ingest: unknown source(s): {', '.join(unknown)}")
        return None

    batch_size = app.config.get("RAG_INGEST_BATCH", 32)
    totals = {"documents": 0, "indexed": 0, "skipped": 0, "chunks": 0, "pruned": 0}

    for source in enabled:
        documents = COLLECTORS[source](app)
        totals["documents"] += len(documents)
        app.logger.info(f"rag-ingest: {source}: {len(documents)} document(s)")
        if dry_run:
            for doc in documents:
                app.logger.info(
                    f"  {doc['doc_id']} ({len(doc['text'])} chars, lang={doc['lang'] or '-'})"
                )
            continue

        if full:
            # Force a re-embed by sending no content hash: the service then has
            # nothing to compare against and re-chunks every document.
            for doc in documents:
                doc["content_hash"] = ""

        for start in range(0, len(documents), batch_size):
            batch = documents[start:start + batch_size]
            last = start + batch_size >= len(documents)
            try:
                result = rag_client.ingest(
                    batch,
                    # the prune manifest rides on the final batch of the source,
                    # so a partially-sent corpus never deletes live documents
                    prune={"source": source, "keep_doc_ids": [d["doc_id"] for d in documents]}
                    if last
                    else None,
                )
            except Exception:
                app.logger.exception(f"rag-ingest: failed to ingest {source}")
                break
            totals["indexed"] += result.get("indexed", 0)
            totals["skipped"] += result.get("skipped", 0)
            totals["chunks"] += result.get("chunks", 0)
            totals["pruned"] += result.get("pruned", 0)

    app.logger.info(
        "rag-ingest: {documents} document(s) collected, {indexed} indexed, "
        "{skipped} unchanged, {chunks} chunk(s), {pruned} pruned".format(**totals)
    )
    return totals


def run_health(app):
    """Report connectivity, index version and backends. Never raises."""
    health = rag_client.health()
    app.logger.info(f"rag-health: {health}")
    if health.get("status") not in ("ok", "degraded"):
        return health
    stats = rag_client.stats()
    corpus = (stats or {}).get("corpus", {})
    app.logger.info(
        "rag-health: {documents} document(s), {chunks} chunk(s), index v{index_version},"
        " embedder={embedder}".format(
            documents=corpus.get("documents"),
            chunks=corpus.get("chunks"),
            index_version=corpus.get("index_version"),
            embedder=corpus.get("embedder"),
        )
    )
    return {"health": health, "stats": stats}
