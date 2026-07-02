"""Shared pytest fixtures/hooks for the test suite.

Every test module keeps a session-scoped Flask app context open for the whole
run (the ``app`` fixture pattern). SQLAlchemy scopes a session - and thus a
pooled DB connection - to each app context, and an uncommitted read leaves that
connection checked out until the session is removed. With enough modules stacked
that exhausts the connection pool (``QueuePool limit ... reached``).

This autouse fixture ends the current test's (idle, read) transaction right
after the test finishes, returning its DB connection to the pool, so at most a
couple of connections are ever in use at once regardless of how many modules are
collected. A rollback (rather than session.remove()) is used so that ORM objects
stay attached to their session - the ordered/stateful suites lazy-load
relationships across test methods and must not be detached. It is a no-op for
the pure (non-DB) suites that never import the app.
"""
import sys

import pytest


@pytest.fixture(autouse=True)
def _return_db_connection():
    yield
    db = getattr(sys.modules.get("apps"), "db", None)
    if db is None:
        return
    try:
        db.session.rollback()
    except Exception:
        pass
