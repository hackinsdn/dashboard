# -*- encoding: utf-8 -*-
"""HTTP surface of the hisdn-rag service.

Deliberately thin: every decision lives in ``pipeline.Engine`` so it can be
tested without an HTTP stack. The blocking work (embedding, generation) runs in
a worker thread, so the event loop stays responsive and the *gate* -- not the
web server -- is what limits concurrency.

The service is never exposed to end users: only the dashboard calls it, with a
bearer token, over a network the deployment restricts.
"""
import logging
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .config import get_settings
from .pipeline import Engine

logging.basicConfig(
    level=os.getenv("RAG_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("ragsvc.api")

app = FastAPI(title="hisdn-rag", version="0.1.0", docs_url=None, redoc_url=None)

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = Engine(get_settings())
    return _engine


def set_engine(engine):
    """Test hook: inject an Engine built with stub backends."""
    global _engine
    _engine = engine


def require_token(authorization: str = Header(default="")):
    """Bearer auth. An unset token means auth is disabled (dev only)."""
    expected = get_settings().service_token
    if not expected:
        return
    presented = authorization[7:] if authorization.startswith("Bearer ") else ""
    if presented != expected:
        raise HTTPException(status_code=401, detail="invalid or missing token")


class Turn(BaseModel):
    role: str
    content: str


class AnswerRequest(BaseModel):
    question: str
    locale: str = "en"
    history: list[Turn] = Field(default_factory=list)
    conversation_id: str | None = None
    top_k: int | None = None


class Document(BaseModel):
    doc_id: str
    title: str = ""
    url: str = ""
    lang: str = ""
    source: str = ""
    content_hash: str = ""
    text: str = ""


class Prune(BaseModel):
    source: str
    keep_doc_ids: list[str] = Field(default_factory=list)


class IngestRequest(BaseModel):
    documents: list[Document] = Field(default_factory=list)
    prune: Prune | None = None


@app.get("/healthz")
def healthz():
    return get_engine().health()


@app.post("/v1/answer", dependencies=[Depends(require_token)])
async def answer(request: AnswerRequest):
    engine = get_engine()
    result = await run_in_threadpool(
        engine.answer,
        request.question,
        locale=request.locale,
        history=[t.model_dump() for t in request.history],
        conversation_id=request.conversation_id,
        top_k=request.top_k,
    )
    # 503 is the signal the dashboard's fallback keys on; refusals are a normal
    # 200 outcome, not an error.
    if result.get("status") == "unavailable":
        raise HTTPException(status_code=503, detail=result)
    return result


@app.post("/v1/ingest", dependencies=[Depends(require_token)])
async def ingest(request: IngestRequest):
    engine = get_engine()
    return await run_in_threadpool(
        engine.ingest,
        [d.model_dump() for d in request.documents],
        request.prune.model_dump() if request.prune else None,
    )


@app.get("/v1/stats", dependencies=[Depends(require_token)])
def stats():
    return get_engine().stats()
