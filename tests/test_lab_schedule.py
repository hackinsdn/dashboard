"""Pytest suite for apps/cli/lab_schedule.py.

Covers alert_expiring_labs (the send/skip branches) and run_delete_expired_labs
(delete, keep-within-tolerance, and the teardown-error path). Mail and the k8s
teardown are monkeypatched; no real email is sent and no cluster is touched.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_lab_schedule.py -v

Both functions scan *all* LabInstances in the (shared, singleton) DB, so this
module neutralises pre-existing instances once at setup and wipes its own lab's
instances before each test, then asserts on this file's users/instances only.
The seeded names are prefixed "ls" to avoid collisions with other modules.
"""
import os
import sys
import tempfile
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# --- isolate the test run from any real data --------------------------------
TEST_DATA_DIR = tempfile.mkdtemp(prefix="hackinsdn_test_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ.setdefault("OPTIONAL_MODULES", "")

# stub the clabernetes controller (needs a 'clabverter' binary at import time)
_fake_clabernetes = types.ModuleType("apps.controllers.clabernetes")


class _StubC9sController:
    def __getattr__(self, name):
        raise NotImplementedError("clabernetes stub - not needed for these tests")


_fake_clabernetes.C9sController = _StubC9sController
sys.modules["apps.controllers.clabernetes"] = _fake_clabernetes

from run import app as flask_app  # noqa: E402
from apps import db  # noqa: E402
from apps.config import app_config  # noqa: E402
from apps.authentication.models import Users  # noqa: E402
from apps.home.models import Labs, LabInstances  # noqa: E402
import apps.cli.lab_schedule as lab_schedule  # noqa: E402
from apps.audit_mixin import utcnow  # noqa: E402

flask_app.config["TESTING"] = True

WARN = app_config.LAB_EXPIRATION_WARN_SEC
TOL = app_config.LAB_EXPIRATION_TOLERANACE_SEC


# --- fixtures ----------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _cleanup_temp_data_dir():
    yield
    import shutil

    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def app():
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()


@pytest.fixture(scope="session")
def ids(app):
    # neutralise any instances seeded by other modules so only ours can match
    LabInstances.query.update({LabInstances.expiration_ts: None})
    db.session.commit()

    user = Users(username="ls_user", password="p", email="ls_user@test.local", category="student")
    noemail = Users(username="ls_noemail", password="p", email="", category="student")
    lab = Labs(title="Ls Lab", description="scheduled lab")
    db.session.add_all([user, noemail, lab])
    db.session.commit()
    return {"user_id": user.id, "user_email": user.email, "noemail_id": noemail.id, "lab_id": lab.id}


@pytest.fixture(autouse=True)
def _clean_lab_instances(app, ids):
    """Each test controls its own set of instances for this file's lab."""
    LabInstances.query.filter_by(lab_id=ids["lab_id"]).delete()
    db.session.commit()
    yield


@pytest.fixture()
def mail_outbox(monkeypatch):
    outbox = []

    class FakeMail:
        def __init__(self, app):
            pass

        def send(self, msg):
            outbox.append(msg)

    monkeypatch.setattr(lab_schedule, "Mail", FakeMail)
    monkeypatch.setitem(flask_app.config, "MAIL_DEFAULT_SENDER", "noreply@test.local")
    return outbox


@pytest.fixture()
def k8s_calls(monkeypatch):
    calls = []
    ns = types.SimpleNamespace(delete_resources_by_name=lambda res: calls.append(res) or [])
    monkeypatch.setattr(lab_schedule, "k8s", ns)
    return calls


# --- helpers -----------------------------------------------------------
def make_instance(ids, expiration_ts, user_id=None, is_deleted=False):
    inst = LabInstances(
        user_id=user_id if user_id is not None else ids["user_id"],
        lab_id=ids["lab_id"],
        is_deleted=is_deleted,
        expiration_ts=expiration_ts,
    )
    inst.k8s_resources = []
    db.session.add(inst)
    db.session.commit()
    return inst


def now_ts():
    return int(utcnow().timestamp())


# --- alert_expiring_labs ------------------------------------------------
class TestAlertExpiringLabs:
    def test_no_send_when_send_email_false(self, app, ids, mail_outbox):
        make_instance(ids, expiration_ts=now_ts() + 3600)  # expiring within 48h
        lab_schedule.alert_expiring_labs(flask_app, send_email=False)
        assert mail_outbox == []

    def test_sends_to_user_with_email(self, app, ids, mail_outbox):
        make_instance(ids, expiration_ts=now_ts() + 3600)
        lab_schedule.alert_expiring_labs(flask_app, send_email=True)
        assert len(mail_outbox) == 1
        assert mail_outbox[0].recipients == [ids["user_email"]]

    def test_no_send_for_user_without_email(self, app, ids, mail_outbox):
        make_instance(ids, expiration_ts=now_ts() + 3600, user_id=ids["noemail_id"])
        lab_schedule.alert_expiring_labs(flask_app, send_email=True)
        assert mail_outbox == []

    def test_missing_user_is_skipped(self, app, ids, mail_outbox):
        make_instance(ids, expiration_ts=now_ts() + 3600, user_id=999999)
        lab_schedule.alert_expiring_labs(flask_app, send_email=True)
        assert mail_outbox == []

    def test_far_future_not_alerted(self, app, ids, mail_outbox):
        make_instance(ids, expiration_ts=now_ts() + WARN + 100000)
        lab_schedule.alert_expiring_labs(flask_app, send_email=True)
        assert mail_outbox == []

    def test_none_expiration_and_deleted_excluded(self, app, ids, mail_outbox):
        make_instance(ids, expiration_ts=None)  # no expiration set
        make_instance(ids, expiration_ts=now_ts() + 3600, is_deleted=True)  # already gone
        lab_schedule.alert_expiring_labs(flask_app, send_email=True)
        assert mail_outbox == []


# --- run_delete_expired_labs --------------------------------------------
class TestRunDeleteExpiredLabs:
    def test_deletes_expired_instance(self, app, ids, k8s_calls):
        inst = make_instance(ids, expiration_ts=now_ts() - TOL - 3600)
        lab_schedule.run_delete_expired_labs(flask_app)

        db.session.refresh(inst)
        assert inst.is_deleted is True
        assert inst.finish_reason == "Lab Expired"
        assert len(k8s_calls) == 1

    def test_within_tolerance_is_kept(self, app, ids, k8s_calls):
        inst = make_instance(ids, expiration_ts=now_ts() - 3600)  # expired recently, still tolerated
        lab_schedule.run_delete_expired_labs(flask_app)

        db.session.refresh(inst)
        assert inst.is_deleted is False
        assert k8s_calls == []

    def test_teardown_error_keeps_instance(self, app, ids, monkeypatch):
        def boom(resources):
            raise RuntimeError("cluster unavailable")

        monkeypatch.setattr(lab_schedule, "k8s", types.SimpleNamespace(delete_resources_by_name=boom))
        inst = make_instance(ids, expiration_ts=now_ts() - TOL - 3600)
        lab_schedule.run_delete_expired_labs(flask_app)

        db.session.refresh(inst)
        assert inst.is_deleted is False
