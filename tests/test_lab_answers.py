"""Pytest suite for the Lab Answers feature.

Mirrors tests/test_lab_categories.py: covers list_lab_answers and
add_answer_sheet in apps/home/routes.py, exercising the LabAnswers and
LabAnswerSheet models - role gating, lab/group filtering and their validation,
answer-sheet scoring (both the numeric-grade and the regex-match branches),
and creating/updating an answer sheet.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_lab_answers.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "la" so they never collide with the
other test modules that share the same singleton app/database (apps/config.py
reads DATA_DIR once at import time, so every module ends up on one DB).
"""
import json
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
from apps.authentication.models import Users, Groups  # noqa: E402
from apps.home.models import Labs, LabAnswers, LabAnswerSheet  # noqa: E402

flask_app.config["TESTING"] = True
flask_app.config["WTF_CSRF_ENABLED"] = False


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
def client(app):
    return app.test_client()


@pytest.fixture(scope="session")
def ids(app):
    """Seed the users/group/lab/answers fixtures shared by every test."""
    admin = Users(username="laadmin", password="admin123", email="laadmin@test.local", category="admin")
    teacher = Users(username="lateacher", password="teach123", email="lateacher@test.local", category="teacher")
    student = Users(username="lastudent", password="stud123", email="lastudent@test.local", category="student")
    student2 = Users(username="lastudent2", password="stud123", email="lastudent2@test.local", category="student")
    db.session.add_all([admin, teacher, student, student2])
    db.session.commit()

    # a group the teacher owns and the student belongs to
    group = Groups(groupname="LaGroup", organization="ORG1")
    group.owners.append(teacher)
    group.members.append(student)
    db.session.add(group)

    lab = Labs(title="Answers Lab", description="lab with answers")
    lab.allowed_groups.append(group)
    lab2 = Labs(title="Sheetless Lab", description="lab without an answer sheet")
    db.session.add_all([lab, lab2])
    db.session.commit()

    # answer sheet for `lab` - question q2 is graded by regex match
    sheet = LabAnswerSheet()
    sheet.lab_id = lab.id
    sheet.set_answers({"q2": "bar"})
    db.session.add(sheet)

    # student's answer: q1 has a numeric grade (90), q2 matches the sheet
    ans1 = LabAnswers(user_id=student.id, lab_id=lab.id)
    ans1.answers = json.dumps({"q1": "foo", "q2": "bar"})
    ans1.grades = json.dumps({"q1": 90})
    # a non-member's answer (used to prove group filtering excludes it)
    ans2 = LabAnswers(user_id=student2.id, lab_id=lab.id)
    ans2.answers = json.dumps({"q1": "ZZZ-notmember"})
    ans2.grades = json.dumps({})
    db.session.add_all([ans1, ans2])
    db.session.commit()

    return {
        "admin_id": admin.id,
        "teacher_id": teacher.id,
        "student_id": student.id,
        "student2_id": student2.id,
        "group_id": group.id,
        "lab_id": lab.id,
        "lab2_id": lab2.id,
    }


# --- helpers -----------------------------------------------------------
def login(client, username, password):
    resp = client.post(
        "/login/",
        data={"identifier": username, "password": password, "login": "1"},
    )
    return resp.status_code == 302


def logout(client):
    client.get("/logout")


# --- list_lab_answers ---------------------------------------------------
class TestListLabAnswers:
    def test_student_is_rejected(self, client, ids):
        logout(client)
        login(client, "lastudent", "stud123")
        resp = client.get("/lab_answers/list")
        assert b"Unauthorized request" in resp.data
        logout(client)

    def test_admin_sees_answers_with_score(self, client, ids):
        login(client, "laadmin", "admin123")
        resp = client.get("/lab_answers/list")
        assert resp.status_code == 200
        # no sheet requested -> score comes from the numeric grade only (90%)
        assert b"90.00" in resp.data

    def test_score_uses_answer_sheet_when_requested(self, client, ids):
        resp = client.get(f"/lab_answers/list?filter_lab={ids['lab_id']}&check_answer_sheet=1")
        assert resp.status_code == 200
        # q1 grade 90 (0.9) + q2 regex match (1.0) over 2 questions -> 95%
        assert b"95.00" in resp.data

    def test_invalid_lab_filter_is_rejected(self, client, ids):
        resp = client.get("/lab_answers/list?filter_lab=does-not-exist")
        assert b"Invalid Lab provided for filtering." in resp.data

    def test_invalid_group_filter_is_rejected(self, client, ids):
        resp = client.get("/lab_answers/list?filter_group=999999")
        assert b"Invalid Group provided for filtering." in resp.data

    def test_check_sheet_without_lab_is_rejected(self, client, ids):
        resp = client.get("/lab_answers/list?check_answer_sheet=1")
        assert b"To check with the Answer Sheet you must provide a Lab" in resp.data

    def test_check_sheet_without_existing_sheet_is_rejected(self, client, ids):
        resp = client.get(f"/lab_answers/list?filter_lab={ids['lab2_id']}&check_answer_sheet=1")
        assert b"No Lab Answer Sheet available" in resp.data

    def test_group_filter_excludes_non_members(self, client, ids):
        resp = client.get(f"/lab_answers/list?filter_group={ids['group_id']}")
        assert resp.status_code == 200
        assert b"90.00" in resp.data                 # the member's answer
        assert b"ZZZ-notmember" not in resp.data      # the non-member's answer
        logout(client)

    def test_teacher_sees_answers_for_owned_group_labs(self, client, ids):
        login(client, "lateacher", "teach123")
        resp = client.get("/lab_answers/list")
        assert resp.status_code == 200
        assert b"90.00" in resp.data
        logout(client)


# --- add_answer_sheet ---------------------------------------------------
class TestAddAnswerSheet:
    def test_get_without_lab_shows_picker(self, client, ids):
        login(client, "laadmin", "admin123")
        resp = client.get("/lab_answers/answer_sheet/")
        assert resp.status_code == 200

    def test_get_invalid_lab_is_rejected(self, client, ids):
        resp = client.get("/lab_answers/answer_sheet/?lab_id=does-not-exist")
        assert b"Invalid Lab provided." in resp.data

    def test_get_valid_lab_prefills_existing(self, client, ids):
        resp = client.get(f"/lab_answers/answer_sheet/?lab_id={ids['lab_id']}")
        assert resp.status_code == 200

    def test_post_creates_new_sheet(self, client, ids):
        resp = client.post(
            f"/lab_answers/answer_sheet/?lab_id={ids['lab2_id']}",
            data={"question": ["q1", "q2"], "answer": ["a1", "a2"]},
        )
        assert b"Lab answer sheet saved!" in resp.data

        sheet = LabAnswerSheet.query.filter_by(lab_id=ids["lab2_id"]).first()
        assert sheet is not None
        assert sheet.answers_dict == {"q1": "a1", "q2": "a2"}

    def test_post_updates_existing_sheet(self, client, ids):
        resp = client.post(
            f"/lab_answers/answer_sheet/?lab_id={ids['lab2_id']}",
            data={"question": ["q1"], "answer": ["changed"]},
        )
        assert b"Lab answer sheet saved!" in resp.data

        sheets = LabAnswerSheet.query.filter_by(lab_id=ids["lab2_id"]).all()
        assert len(sheets) == 1  # updated in place, not duplicated
        assert sheets[0].answers_dict == {"q1": "changed"}
        logout(client)
