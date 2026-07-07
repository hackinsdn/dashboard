"""Pytest suite for LTI AGS grade passback (apps/lti/grades.py).

Covers: the shared compute_lab_score() helper (extracted from the teacher
answers listing), the friendly-comment builder (including the no-answers
case that must still list every answer-sheet question), persistence of the
AGS launch context at /lti/launch/ (create + refresh, skip without the AGS
claim), and the finished-lab-infos trigger end to end with a faked AGS
service: score+comment with an answer sheet, zero score when nothing was
answered, comment-only (PendingManual) without a sheet, context selection
via the custom next_url deep link, and every skip/failure path rendering
the congratulations page regardless.

No LMS is required: ServiceConnector/AssignmentsGradesService/DbToolConf
are monkeypatched inside apps.lti.grades, and launches use a fake
FlaskMessageLaunch.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_lti_grades.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames/e-mails are all prefixed "lg" so they never collide with
the other test modules that share the same singleton app/database
(apps/config.py reads DATA_DIR once at import time, so every module ends up
on one DB).
"""
import json
import os
import sys
import tempfile
import types
from types import SimpleNamespace

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
from apps.authentication.models import Users  # noqa: E402
from apps.home.models import LabAnswers, LabAnswerSheet, Labs  # noqa: E402
from apps.utils import compute_lab_score  # noqa: E402

from apps.lti import blueprint as lti_blueprint  # noqa: E402
import apps.lti.routes  # noqa: E402,F401
from apps.lti.grades import build_answers_comment  # noqa: E402
from apps.lti.models import LtiLaunchContext  # noqa: E402

if "lti_blueprint" not in flask_app.blueprints:
    flask_app.register_blueprint(lti_blueprint)

flask_app.config["TESTING"] = True
flask_app.config["WTF_CSRF_ENABLED"] = False
flask_app.config["ENABLE_LTI"] = True

with flask_app.app_context():
    db.create_all()

ISSUER = "https://lg-moodle.example"
AGS_CLAIM_NAME = "https://purl.imsglobal.org/spec/lti-ags/claim/endpoint"
SCORE_SCOPE = "https://purl.imsglobal.org/spec/lti-ags/scope/score"


def make_ags_claim(lineitem="https://lg-moodle.example/lineitem/1", scopes=(SCORE_SCOPE,)):
    return {"scope": list(scopes), "lineitem": lineitem,
            "lineitems": "https://lg-moodle.example/lineitems"}


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def client():
    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            yield test_client


@pytest.fixture(scope="module")
def seed():
    with flask_app.app_context():
        lti_user = Users(username="lg_lti", password="lg-pass",
                         email="lg-lti@test.local", category="student",
                         issuer=ISSUER, subject="lg-sub-1")
        local_user = Users(username="lg_local", password="lg-pass",
                           email="lg-local@test.local", category="student")
        lab_sheet = Labs(title="LG Sheet Lab", description="graded")
        lab_sheet_noanswers = Labs(title="LG Empty Lab", description="graded, unanswered")
        lab_nosheet = Labs(title="LG Freeform Lab", description="ungraded")
        db.session.add_all([lti_user, local_user, lab_sheet, lab_sheet_noanswers, lab_nosheet])
        db.session.commit()

        sheet = LabAnswerSheet(lab_id=lab_sheet.id)
        sheet.set_answers({"q1_answer": "42", "q2_answer": "4[0-9]"})
        sheet2 = LabAnswerSheet(lab_id=lab_sheet_noanswers.id)
        sheet2.set_answers({"e1_answer": "yes", "e2_answer": "no"})
        db.session.add_all([sheet, sheet2])

        answers = LabAnswers(user_id=lti_user.id, lab_id=lab_sheet.id)
        answers.answers = json.dumps({"q1_answer": "42", "q2_answer": "99"})
        answers_nosheet = LabAnswers(user_id=lti_user.id, lab_id=lab_nosheet.id)
        answers_nosheet.answers = json.dumps({"free_q": "my essay"})
        db.session.add_all([answers, answers_nosheet])

        context = LtiLaunchContext(
            user_id=lti_user.id, issuer=ISSUER, client_id="lg-client",
            deployment_id="1", resource_link_id="rl-1",
        )
        context.ags = make_ags_claim()
        db.session.add(context)
        db.session.commit()
        yield {
            "lti_user_id": lti_user.id,
            "lab_sheet": lab_sheet.id,
            "lab_sheet_noanswers": lab_sheet_noanswers.id,
            "lab_nosheet": lab_nosheet.id,
            "context_id": context.id,
        }


def login(client, username, password="lg-pass"):
    resp = client.post(
        "/login/",
        data={"identifier": username, "password": password, "login": "1"},
    )
    return resp.status_code == 302


# --- fake AGS plumbing ----------------------------------------------------------

class FakeAGS:
    calls = []          # list of {"claim":..., "payload":..., "lineitem_id":...}
    raise_exc = None
    platform_lineitems = []   # dicts the fake "lineitems collection" holds
    created_lineitems = []

    def __init__(self, connector, service_data):
        self.service_data = service_data

    # scope checks mirror pylti1p3's AssignmentsGradesService
    def can_read_lineitem(self):
        scopes = self.service_data.get("scope") or []
        return (
            "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly" in scopes
            or "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem" in scopes
        )

    def can_create_lineitem(self):
        scopes = self.service_data.get("scope") or []
        return "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem" in scopes

    def get_lineitems(self):
        return [dict(item) for item in FakeAGS.platform_lineitems]

    def find_or_create_lineitem(self, new_lineitem):
        new_lineitem.set_id("https://lg-moodle.example/lineitem/CREATED")
        FakeAGS.created_lineitems.append(new_lineitem)
        return new_lineitem

    def put_grade(self, grade, lineitem=None):
        if FakeAGS.raise_exc:
            raise FakeAGS.raise_exc
        FakeAGS.calls.append({
            "claim": self.service_data,
            "payload": json.loads(grade.get_value()),
            "lineitem_id": lineitem.get_id() if lineitem else None,
        })
        return {"body": "", "headers": {}}


@pytest.fixture()
def fake_ags(monkeypatch):
    monkeypatch.setattr("apps.lti.grades.ServiceConnector", lambda reg, sess: None)
    monkeypatch.setattr("apps.lti.grades.AssignmentsGradesService", FakeAGS)
    monkeypatch.setattr(
        "apps.lti.grades.DbToolConf",
        lambda: SimpleNamespace(find_registration_by_params=lambda iss, cid: object()),
    )
    FakeAGS.calls = []
    FakeAGS.raise_exc = None
    FakeAGS.platform_lineitems = []
    FakeAGS.created_lineitems = []
    yield FakeAGS


# --- unit: score computation ------------------------------------------------------

class TestComputeLabScore:
    def test_regex_and_unanswered(self):
        sheet = {"a": "42", "b": "4[0-9]", "c": "x"}
        answers = {"a": "42", "b": "45"}  # c unanswered
        score, correct, total = compute_lab_score(answers, {}, sheet)
        assert (round(score, 2), correct, total) == (66.67, 2, 3)

    def test_manual_grade_overrides_regex(self):
        score, correct, total = compute_lab_score(
            {"a": "wrong"}, {"a": 50}, {"a": "42"})
        assert (score, total) == (50.0, 1)

    def test_invalid_regex_ignored(self):
        score, _, _ = compute_lab_score({"a": "42"}, {}, {"a": "4[2"})
        assert score == 0.0

    def test_no_gradable_questions(self):
        assert compute_lab_score({"a": "42"}, {}, {}) == (None, 0, 0)
        assert compute_lab_score({}, {}, {}) == (None, 0, 0)


# --- unit: comment builder ---------------------------------------------------------

class TestBuildAnswersComment:
    def test_mix_answered_unanswered(self):
        comment = build_answers_comment(
            "My Lab", {"q1": "42"}, {"q1": "42", "q2": ".*"}, (50.0, 1, 2))
        assert 'Lab "My Lab"' in comment
        assert "- q1: 42" in comment
        assert "- q2: (not answered)" in comment
        assert "Auto-grade from answer sheet: 50.00% (1 of 2 correct)" in comment

    def test_no_answers_lists_sheet_questions(self):
        comment = build_answers_comment(
            "My Lab", {}, {"q1": "42", "q2": ".*"}, (0.0, 0, 2))
        assert "- q1: (not answered)" in comment
        assert "- q2: (not answered)" in comment
        assert "0.00%" in comment

    def test_no_data_at_all(self):
        comment = build_answers_comment("My Lab", {}, {}, (None, 0, 0))
        assert "No answers were submitted for this lab." in comment
        assert "Auto-grade" not in comment

    def test_no_sheet_no_score_line(self):
        comment = build_answers_comment(
            "My Lab", {"free_q": "essay"}, {}, (None, 0, 0))
        assert "- free_q: essay" in comment
        assert "Auto-grade" not in comment


# --- launch context persistence -----------------------------------------------------

class _FakeMessageLaunch:
    launch_data = {}

    def __init__(self, *args, **kwargs):
        pass

    def get_launch_data(self):
        return dict(self.launch_data)


class TestLaunchContextPersistence:
    ISSUER2 = "https://lg-ctx.example"

    def _claims(self, ags=True, next_url=None, resource_link="rl-ctx-1"):
        claims = {
            "iss": self.ISSUER2,
            "sub": "lg-ctx-sub",
            "aud": ["lg-ctx-client"],
            "email": "lg-ctx@test.local",
            "https://purl.imsglobal.org/spec/lti/claim/deployment_id": "dep-1",
            "https://purl.imsglobal.org/spec/lti/claim/resource_link": {"id": resource_link},
            "https://purl.imsglobal.org/spec/lti/claim/roles": [],
        }
        if ags:
            claims[AGS_CLAIM_NAME] = make_ags_claim("https://lg-ctx.example/li/9")
        if next_url:
            claims["https://purl.imsglobal.org/spec/lti/claim/custom"] = {"next_url": next_url}
        return claims

    @pytest.fixture(autouse=True)
    def _fake_launch(self, monkeypatch):
        monkeypatch.setattr("apps.lti.routes.FlaskMessageLaunch", _FakeMessageLaunch)
        yield

    def test_launch_stores_context(self, client):
        _FakeMessageLaunch.launch_data = self._claims(next_url="/labs/some-lab")
        resp = client.post("/lti/launch/")
        assert resp.status_code == 302
        user = Users.query.filter_by(issuer=self.ISSUER2, subject="lg-ctx-sub").first()
        contexts = LtiLaunchContext.query.filter_by(user_id=user.id).all()
        assert len(contexts) == 1
        context = contexts[0]
        assert context.client_id == "lg-ctx-client"
        assert context.deployment_id == "dep-1"
        assert context.resource_link_id == "rl-ctx-1"
        assert context.custom_next_url == "/labs/some-lab"
        assert context.ags["lineitem"] == "https://lg-ctx.example/li/9"

    def test_relaunch_refreshes_not_duplicates(self, client):
        _FakeMessageLaunch.launch_data = self._claims(next_url="/labs/other-lab")
        client.post("/lti/launch/")
        user = Users.query.filter_by(issuer=self.ISSUER2, subject="lg-ctx-sub").first()
        contexts = LtiLaunchContext.query.filter_by(user_id=user.id).all()
        assert len(contexts) == 1
        assert contexts[0].custom_next_url == "/labs/other-lab"

    def test_second_resource_link_gets_own_row(self, client):
        _FakeMessageLaunch.launch_data = self._claims(resource_link="rl-ctx-2")
        client.post("/lti/launch/")
        user = Users.query.filter_by(issuer=self.ISSUER2, subject="lg-ctx-sub").first()
        assert LtiLaunchContext.query.filter_by(user_id=user.id).count() == 2

    def test_launch_without_ags_claim_stores_nothing(self, client):
        _FakeMessageLaunch.launch_data = dict(
            self._claims(ags=False, resource_link="rl-ctx-3"),
            sub="lg-ctx-sub-noags", email="lg-ctx2@test.local",
        )
        client.post("/lti/launch/")
        user = Users.query.filter_by(issuer=self.ISSUER2, subject="lg-ctx-sub-noags").first()
        assert LtiLaunchContext.query.filter_by(user_id=user.id).count() == 0


# --- finished-lab-infos trigger --------------------------------------------------------

class TestFinishedLabTrigger:
    def test_sheet_and_answers_sends_score_and_comment(self, client, seed, fake_ags):
        assert login(client, "lg_lti")
        resp = client.get(f"/finished-lab-infos/{seed['lab_sheet']}")
        assert resp.status_code == 200
        assert b"sent to your course gradebook" in resp.data

        assert len(fake_ags.calls) == 1
        payload = fake_ags.calls[0]["payload"]
        assert payload["userId"] == "lg-sub-1"
        assert payload["scoreGiven"] == 50.0  # q1 correct, q2 wrong
        assert payload["scoreMaximum"] == 100
        assert payload["gradingProgress"] == "FullyGraded"
        assert payload["activityProgress"] == "Completed"
        assert "- q1_answer: 42" in payload["comment"]
        assert "- q2_answer: 99" in payload["comment"]

    def test_sheet_without_answers_sends_zero_and_names(self, client, seed, fake_ags):
        assert login(client, "lg_lti")
        resp = client.get(f"/finished-lab-infos/{seed['lab_sheet_noanswers']}")
        assert resp.status_code == 200
        payload = fake_ags.calls[0]["payload"]
        assert payload["scoreGiven"] == 0.0
        assert "- e1_answer: (not answered)" in payload["comment"]
        assert "- e2_answer: (not answered)" in payload["comment"]

    def test_no_sheet_sends_comment_only(self, client, seed, fake_ags):
        assert login(client, "lg_lti")
        resp = client.get(f"/finished-lab-infos/{seed['lab_nosheet']}")
        assert resp.status_code == 200
        payload = fake_ags.calls[0]["payload"]
        assert "scoreGiven" not in payload
        assert "scoreMaximum" not in payload
        assert payload["gradingProgress"] == "PendingManual"
        assert "- free_q: my essay" in payload["comment"]

    def test_non_lti_user_is_untouched(self, client, seed, fake_ags):
        assert login(client, "lg_local")
        resp = client.get(f"/finished-lab-infos/{seed['lab_sheet']}")
        assert resp.status_code == 200
        assert b"sent to your course gradebook" not in resp.data
        assert fake_ags.calls == []

    def test_context_without_score_scope_skipped(self, client, seed, fake_ags):
        context = db.session.get(LtiLaunchContext, seed["context_id"])
        original = context.ags
        context.ags = make_ags_claim(scopes=())
        db.session.commit()
        try:
            assert login(client, "lg_lti")
            resp = client.get(f"/finished-lab-infos/{seed['lab_sheet']}")
            assert resp.status_code == 200
            assert fake_ags.calls == []
        finally:
            context.ags = original
            db.session.commit()

    def test_context_without_lineitem_skipped(self, client, seed, fake_ags):
        context = db.session.get(LtiLaunchContext, seed["context_id"])
        original = context.ags
        context.ags = make_ags_claim(lineitem=None)
        db.session.commit()
        try:
            assert login(client, "lg_lti")
            resp = client.get(f"/finished-lab-infos/{seed['lab_sheet']}")
            assert resp.status_code == 200
            assert fake_ags.calls == []
        finally:
            context.ags = original
            db.session.commit()

    def test_platform_error_never_breaks_page(self, client, seed, fake_ags, caplog):
        fake_ags.raise_exc = RuntimeError("platform down")
        assert login(client, "lg_lti")
        with caplog.at_level("WARNING"):
            resp = client.get(f"/finished-lab-infos/{seed['lab_sheet']}")
        assert resp.status_code == 200
        assert b"Congratulations" in resp.data
        assert "LTI grade passback failed" in caplog.text

    def test_lti_service_rejection_logged_politely(self, client, seed, fake_ags, caplog):
        from pylti1p3.exception import LtiServiceException
        fake_response = SimpleNamespace(
            url="https://lg-moodle.example/scores", status_code=400, text="not gradable")
        fake_ags.raise_exc = LtiServiceException(fake_response)
        assert login(client, "lg_lti")
        with caplog.at_level("INFO"):
            resp = client.get(f"/finished-lab-infos/{seed['lab_sheet']}")
        assert resp.status_code == 200
        assert "rejected by platform" in caplog.text

    def test_moodle_grade_sync_claim_resolved_from_collection(self, client, seed, fake_ags):
        """Regression: Moodle with plain "grade sync" (no column management)
        sends no default lineitem - only the lineitems collection URL plus
        lineitem.readonly/result.readonly/score scopes. The column must be
        located in the collection by resource link id."""
        context = db.session.get(LtiLaunchContext, seed["context_id"])
        original = context.ags
        context.ags = {
            "scope": [
                "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly",
                "https://purl.imsglobal.org/spec/lti-ags/scope/result.readonly",
                SCORE_SCOPE,
            ],
            "lineitems": "https://lg-moodle.example/mod/lti/services.php/2/lineitems?type_id=1",
        }
        db.session.commit()
        fake_ags.platform_lineitems = [
            {"id": "https://lg-moodle.example/li/42", "resourceLinkId": "rl-1",
             "label": "HackInSDN", "scoreMaximum": 100},
        ]
        try:
            assert login(client, "lg_lti")
            resp = client.get(f"/finished-lab-infos/{seed['lab_sheet']}")
            assert resp.status_code == 200
            assert b"sent to your course gradebook" in resp.data
            assert len(fake_ags.calls) == 1
            assert fake_ags.calls[0]["lineitem_id"] == "https://lg-moodle.example/li/42"
            assert fake_ags.calls[0]["payload"]["scoreGiven"] == 50.0
        finally:
            context.ags = original
            db.session.commit()

    def test_collection_without_match_and_no_create_scope_skipped(self, client, seed, fake_ags):
        context = db.session.get(LtiLaunchContext, seed["context_id"])
        original = context.ags
        context.ags = {
            "scope": ["https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly",
                      SCORE_SCOPE],
            "lineitems": "https://lg-moodle.example/lineitems",
        }
        db.session.commit()
        try:
            assert login(client, "lg_lti")
            resp = client.get(f"/finished-lab-infos/{seed['lab_sheet']}")
            assert resp.status_code == 200
            assert fake_ags.calls == []
        finally:
            context.ags = original
            db.session.commit()

    def test_collection_without_match_creates_column_with_full_scope(self, client, seed, fake_ags):
        context = db.session.get(LtiLaunchContext, seed["context_id"])
        original = context.ags
        context.ags = {
            "scope": ["https://purl.imsglobal.org/spec/lti-ags/scope/lineitem",
                      SCORE_SCOPE],
            "lineitems": "https://lg-moodle.example/lineitems",
        }
        db.session.commit()
        try:
            assert login(client, "lg_lti")
            resp = client.get(f"/finished-lab-infos/{seed['lab_sheet']}")
            assert resp.status_code == 200
            assert len(fake_ags.calls) == 1
            assert fake_ags.calls[0]["lineitem_id"] == "https://lg-moodle.example/lineitem/CREATED"
            created = fake_ags.created_lineitems[0]
            assert created.get_tag() == f"hackinsdn-lab-{seed['lab_sheet']}"
            assert created.get_score_maximum() == 100
        finally:
            context.ags = original
            db.session.commit()

    def test_context_selection_prefers_next_url_match(self, client, seed, fake_ags):
        # a second, NEWER context without a deep link...
        newer = LtiLaunchContext(
            user_id=seed["lti_user_id"], issuer=ISSUER, client_id="lg-client",
            deployment_id="1", resource_link_id="rl-2",
        )
        newer.ags = make_ags_claim("https://lg-moodle.example/lineitem/OTHER")
        # ...while the older one deep-links to the finished lab
        deep = db.session.get(LtiLaunchContext, seed["context_id"])
        deep.custom_next_url = f"/labs/{seed['lab_sheet']}"
        db.session.add(newer)
        db.session.commit()
        try:
            assert login(client, "lg_lti")
            client.get(f"/finished-lab-infos/{seed['lab_sheet']}")
            assert fake_ags.calls[0]["claim"]["lineitem"] == "https://lg-moodle.example/lineitem/1"
        finally:
            deep.custom_next_url = None
            db.session.delete(newer)
            db.session.commit()

    def test_disabled_lti_module_skips_send(self, client, seed, fake_ags):
        flask_app.config["ENABLE_LTI"] = False
        try:
            assert login(client, "lg_lti")
            resp = client.get(f"/finished-lab-infos/{seed['lab_sheet']}")
            assert resp.status_code == 200
            assert fake_ags.calls == []
        finally:
            flask_app.config["ENABLE_LTI"] = True
