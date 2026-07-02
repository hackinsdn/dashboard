"""Pytest suite for the /api lab-answers endpoints.

Mirrors tests/test_lab_categories.py: covers get_lab_answers,
save_lab_answers, save_grades_comments and check_lab_answer in
apps/api/routes.py - role/ownership gating, content validation, grade-range
validation, the answer-sheet requirement, and the scoring happy path.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_api_lab_answers.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "ae" so they never collide with the
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
from apps.home.models import Labs, LabInstances, LabAnswers, LabAnswerSheet  # noqa: E402

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
    admin = Users(username="aeadmin", password="admin123", email="aeadmin@test.local", category="admin")
    teacher = Users(username="aeteacher", password="teach123", email="aeteacher@test.local", category="teacher")
    teacher2 = Users(username="aeteacher2", password="teach123", email="aeteacher2@test.local", category="teacher")
    student = Users(username="aestudent", password="stud123", email="aestudent@test.local", category="student")
    other = Users(username="aeother", password="stud123", email="aeother@test.local", category="student")
    pending = Users(username="aepending", password="pend123", email="aepending@test.local", category="user")
    db.session.add_all([admin, teacher, teacher2, student, other, pending])
    db.session.commit()

    group = Groups(groupname="AeGroup", organization="ORG1")
    group.owners.append(teacher)
    group.members.append(student)
    db.session.add(group)

    lab = Labs(title="Ae Lab", description="lab with a sheet")
    lab.allowed_groups.append(group)
    lab2 = Labs(title="Ae Lab No Sheet", description="lab without a sheet")
    lab2.allowed_groups.append(group)
    db.session.add_all([lab, lab2])
    db.session.commit()

    sheet = LabAnswerSheet()
    sheet.lab_id = lab.id
    sheet.set_answers({"q1": "foo"})
    db.session.add(sheet)

    inst = LabInstances(user_id=student.id, lab_id=lab.id, is_deleted=False)
    inst.k8s_resources = []
    # the student's own answer (mutated by the save/grade tests)
    answer = LabAnswers(user_id=student.id, lab_id=lab.id)
    answer.answers = json.dumps({"q1": "foo"})
    answer.grades = json.dumps({})
    # a dedicated answer for the check_lab_answer scoring test (never mutated)
    answer_check = LabAnswers(user_id=other.id, lab_id=lab.id)
    answer_check.answers = json.dumps({"q1": "foo"})
    answer_check.grades = json.dumps({})
    db.session.add_all([inst, answer, answer_check])
    db.session.commit()

    return {
        "admin_id": admin.id,
        "teacher_id": teacher.id,
        "teacher2_id": teacher2.id,
        "student_id": student.id,
        "other_id": other.id,
        "lab_id": lab.id,
        "lab2_id": lab2.id,
        "inst_id": inst.id,
        "answer_id": answer.id,
        "answer_check_id": answer_check.id,
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


# --- get_lab_answers ----------------------------------------------------
class TestGetLabAnswers:
    def test_unapproved_user_is_rejected(self, client, ids):
        logout(client)
        login(client, "aepending", "pend123")
        resp = client.get(f"/api/lab_answers/{ids['inst_id']}")
        assert resp.status_code == 404
        logout(client)

    def test_missing_instance(self, client, ids):
        login(client, "aestudent", "stud123")
        resp = client.get("/api/lab_answers/does-not-exist")
        assert resp.status_code == 404

    def test_non_owner_is_rejected(self, client, ids):
        logout(client)
        login(client, "aeother", "stud123")
        resp = client.get(f"/api/lab_answers/{ids['inst_id']}")
        assert resp.status_code == 401
        logout(client)

    def test_owner_gets_answers(self, client, ids):
        login(client, "aestudent", "stud123")
        resp = client.get(f"/api/lab_answers/{ids['inst_id']}")
        assert resp.status_code == 200
        assert resp.get_json()["result"] == {"q1": "foo"}
        logout(client)


# --- save_lab_answers ---------------------------------------------------
class TestSaveLabAnswers:
    def test_unapproved_user_is_rejected(self, client, ids):
        login(client, "aepending", "pend123")
        resp = client.post(f"/api/lab_answers/{ids['inst_id']}", json={"q1": "x"})
        assert resp.status_code == 404
        logout(client)

    def test_empty_content_is_rejected(self, client, ids):
        login(client, "aestudent", "stud123")
        resp = client.post(f"/api/lab_answers/{ids['inst_id']}")
        assert resp.status_code == 400

    def test_missing_instance(self, client, ids):
        resp = client.post("/api/lab_answers/does-not-exist", json={"q1": "x"})
        assert resp.status_code == 404

    def test_non_owner_is_rejected(self, client, ids):
        logout(client)
        login(client, "aeother", "stud123")
        resp = client.post(f"/api/lab_answers/{ids['inst_id']}", json={"q1": "x"})
        assert resp.status_code == 401
        logout(client)

    def test_owner_saves_answers(self, client, ids):
        login(client, "aestudent", "stud123")
        resp = client.post(f"/api/lab_answers/{ids['inst_id']}", json={"q1": "myans"})
        assert resp.status_code == 200

        answer = db.session.get(LabAnswers, ids["answer_id"])
        assert answer.answers_dict == {"q1": "myans"}
        logout(client)


# --- save_grades_comments -----------------------------------------------
class TestSaveGradesComments:
    def test_non_staff_is_rejected(self, client, ids):
        login(client, "aestudent", "stud123")
        resp = client.post(f"/api/lab_answers/grades_comments/{ids['answer_id']}", json={"grades": {"q1": 50}})
        assert resp.status_code == 401
        logout(client)

    def test_missing_answer(self, client, ids):
        login(client, "aeadmin", "admin123")
        resp = client.post("/api/lab_answers/grades_comments/999999", json={"grades": {}})
        assert resp.status_code == 404

    def test_teacher_not_owner_is_rejected(self, client, ids):
        logout(client)
        login(client, "aeteacher2", "teach123")
        resp = client.post(f"/api/lab_answers/grades_comments/{ids['answer_id']}", json={"grades": {"q1": 50}})
        assert resp.status_code == 401
        logout(client)

    def test_admin_empty_content_is_rejected(self, client, ids):
        login(client, "aeadmin", "admin123")
        resp = client.post(f"/api/lab_answers/grades_comments/{ids['answer_id']}")
        assert resp.status_code == 400

    def test_admin_invalid_grade_is_rejected(self, client, ids):
        resp = client.post(f"/api/lab_answers/grades_comments/{ids['answer_id']}", json={"grades": {"q1": 150}})
        assert resp.status_code == 400
        assert b"Invalid grade" in resp.data

    def test_admin_saves_grades(self, client, ids):
        resp = client.post(
            f"/api/lab_answers/grades_comments/{ids['answer_id']}",
            json={"grades": {"q1": 80}, "comments": {"q1": "good"}},
        )
        assert resp.status_code == 200

        answer = db.session.get(LabAnswers, ids["answer_id"])
        assert answer.grades_dict == {"q1": 80}
        logout(client)


# --- check_lab_answer ---------------------------------------------------
class TestCheckLabAnswer:
    def test_non_staff_is_rejected(self, client, ids):
        login(client, "aestudent", "stud123")
        resp = client.get(f"/api/lab_answers/check/{ids['lab_id']}/{ids['answer_check_id']}")
        assert resp.status_code == 401
        logout(client)

    def test_unknown_lab_is_rejected(self, client, ids):
        login(client, "aeteacher", "teach123")
        resp = client.get(f"/api/lab_answers/check/does-not-exist/{ids['answer_check_id']}")
        assert resp.status_code == 401

    def test_lab_outside_privileged_groups_is_rejected(self, client, ids):
        logout(client)
        login(client, "aeteacher2", "teach123")
        resp = client.get(f"/api/lab_answers/check/{ids['lab_id']}/{ids['answer_check_id']}")
        assert resp.status_code == 401
        logout(client)

    def test_missing_answer_is_rejected(self, client, ids):
        login(client, "aeteacher", "teach123")
        resp = client.get(f"/api/lab_answers/check/{ids['lab_id']}/999999")
        assert resp.status_code == 401

    def test_lab_without_sheet_is_rejected(self, client, ids):
        resp = client.get(f"/api/lab_answers/check/{ids['lab2_id']}/{ids['answer_check_id']}")
        assert resp.status_code == 400
        assert b"No Lab Answer Sheet available" in resp.data

    def test_score_is_returned(self, client, ids):
        resp = client.get(f"/api/lab_answers/check/{ids['lab_id']}/{ids['answer_check_id']}")
        assert resp.status_code == 200
        assert resp.get_json()["result"] == "100.00"
        logout(client)
