# -*- encoding: utf-8 -*-
"""hisdn-rag: the local RAG assistant service for the HackInSDN dashboard.

The modules here are deliberately free of hard third-party dependencies: only
``api`` needs FastAPI, and the heavy model libraries are imported lazily by the
backend that uses them. That keeps the retrieval/grounding logic unit-testable
without downloading any weights.
"""

__version__ = "0.1.0"
