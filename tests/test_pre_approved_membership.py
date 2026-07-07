"""Pytest suite for pre-approved group membership at user creation.

Covers the Users after_insert listener (apps/authentication/models.py:
add_user_to_pre_approved_groups) that opportunistically joins brand-new
users - regardless of how they are created: LTI, OAuth or local signup -
to every group whose pre-approved list contains their e-mail, the
find_pre_approved_groups() helper, the composition with the *unchanged*
check_pre_approved() on an LTI launch (membership from the listener +
category promotion from the login flow, in one transaction), and the
`flask cli sync-pre-approved-users` backfill command (--dry-run and
--promote included).

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_pre_approved_membership.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames/e-mails are all prefixed "pa" so they never collide with
the other test modules that share the same singleton app/database
(apps/config.py reads DATA_DIR once at import time, so every module ends up
on one DB).
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
from apps.authentication.models import Groups, Users  # noqa: E402
from apps.utils import find_pre_approved_groups  # noqa: E402

# the LTI launch integration test needs the optional lti blueprint
# (module import happens at pytest collection time, before any request)
from apps.lti import blueprint as lti_blueprint  # noqa: E402
import apps.lti.routes  # noqa: E402,F401

if "lti_blueprint" not in flask_app.blueprints:
    flask_app.register_blueprint(lti_blueprint)

flask_app.config["TESTING"] = True
flask_app.config["WTF_CSRF_ENABLED"] = False

with flask_app.app_context():
    db.create_all()

LTI_ISSUER = "https://pa-moodle.example"


@pytest.fixture()
def client():
    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            yield test_client


@pytest.fixture(scope="module")
def seed():
    """Two ORG groups + one SYSTEM group, all with pre-approved lists."""
    with flask_app.app_context():
        group_a = Groups(groupname="PaGroupA", organization="PA-ORG")
        group_a.set_approved_users(["pa-new@test.local", "pa-both@test.local",
                                    "pa-lti@test.local", "pa-teacher@test.local",
                                    "pa-cli@test.local", "pa-deleted@test.local"])
        group_b = Groups(groupname="PaGroupB", organization="PA-ORG")
        group_b.set_approved_users(["pa-both@test.local", "pa-member@test.local"])
        group_sys = Groups(groupname="PaSystem", organization="SYSTEM")
        group_sys.set_approved_users(["pa-new@test.local"])
        db.session.add_all([group_a, group_b, group_sys])
        db.session.commit()
        yield {"a": group_a.id, "b": group_b.id, "sys": group_sys.id}


def _group(gid):
    return db.session.get(Groups, gid)


# --- helper -------------------------------------------------------------------

class TestFindPreApprovedGroups:
    def test_matches_and_edge_cases(self, client, seed):
        names = sorted(g.groupname for g in find_pre_approved_groups("pa-new@test.local"))
        assert names == ["PaGroupA", "PaSystem"]  # helper is org-agnostic
        assert find_pre_approved_groups("pa-nobody@test.local") == []
        assert find_pre_approved_groups(None) == []
        assert find_pre_approved_groups("") == []


# --- after_insert listener ------------------------------------------------------

class TestAfterInsertListener:
    def test_new_user_joins_matching_org_groups_only(self, client, seed):
        user = Users(username="pa_new", password="p", email="pa-new@test.local")
        db.session.add(user)
        db.session.commit()
        group_names = {g.groupname for g in user.member_of_groups}
        assert "PaGroupA" in group_names
        # SYSTEM groups are skipped even when the e-mail is on their list
        assert "PaSystem" not in group_names
        # category is NOT touched by the listener itself
        assert user.category == "user"

    def test_new_user_in_two_lists_joins_both(self, client, seed):
        user = Users(username="pa_both", password="p", email="pa-both@test.local")
        db.session.add(user)
        db.session.commit()
        group_names = {g.groupname for g in user.member_of_groups}
        assert {"PaGroupA", "PaGroupB"} <= group_names

    def test_user_without_email_gets_nothing(self, client, seed):
        user = Users(username="pa_noemail", password="p")
        db.session.add(user)
        db.session.commit()
        assert {g.groupname for g in user.member_of_groups} & {"PaGroupA", "PaGroupB"} == set()

    def test_non_matching_email_gets_nothing(self, client, seed):
        user = Users(username="pa_other", password="p", email="pa-other@test.local")
        db.session.add(user)
        db.session.commit()
        assert {g.groupname for g in user.member_of_groups} & {"PaGroupA", "PaGroupB"} == set()


# --- LTI launch composition (listener + unchanged check_pre_approved) ------------

class _FakeMessageLaunch:
    launch_data = {}

    def __init__(self, *args, **kwargs):
        pass

    def get_launch_data(self):
        return dict(self.launch_data)


class TestLtiLaunchComposition:
    def test_first_launch_gets_membership_and_student_category(self, client, seed, monkeypatch):
        monkeypatch.setattr("apps.lti.routes.FlaskMessageLaunch", _FakeMessageLaunch)
        # no LTI roles at all: the student category must come from the
        # unchanged check_pre_approved() seeing the listener's membership
        _FakeMessageLaunch.launch_data = {
            "iss": LTI_ISSUER,
            "sub": "pa-lti-sub",
            "email": "pa-lti@test.local",
            "given_name": "Pre",
            "family_name": "Approved",
            "https://purl.imsglobal.org/spec/lti/claim/roles": [],
        }
        resp = client.post("/lti/launch/")
        assert resp.status_code == 302

        user = Users.query.filter_by(issuer=LTI_ISSUER, subject="pa-lti-sub").first()
        assert user is not None
        assert "PaGroupA" in {g.groupname for g in user.member_of_groups}
        assert user.category == "student"


# --- CLI backfill ----------------------------------------------------------------

class TestCliSync:
    @pytest.fixture(scope="class")
    def cli_seed(self, seed):
        """Pre-existing users the listener never saw (simulated by removing
        the memberships it created)."""
        with flask_app.app_context():
            teacher = Users(username="pa_teacher", password="p",
                            email="pa-teacher@test.local", category="teacher")
            plain = Users(username="pa_cli", password="p",
                          email="pa-cli@test.local", category="user")
            member = Users(username="pa_member", password="p",
                           email="pa-member@test.local", category="student")
            deleted = Users(username="pa_deleted", password="p",
                            email="pa-deleted@test.local", is_deleted=True)
            db.session.add_all([teacher, plain, member, deleted])
            db.session.commit()
            # strip listener-added memberships so these behave like accounts
            # that existed before their e-mails were put on the lists
            for user in (teacher, plain, deleted):
                for group in list(user.member_of_groups):
                    if group.groupname in ("PaGroupA", "PaGroupB"):
                        group.members.remove(user)
            db.session.commit()
            # pa_member keeps its PaGroupB membership (already-member case)
            assert "PaGroupB" in {g.groupname for g in member.member_of_groups}
            yield

    def _run(self, *args):
        runner = flask_app.test_cli_runner()
        result = runner.invoke(args=["cli", "sync-pre-approved-users", *args])
        assert result.exception is None, result.output
        return result.output

    def test_dry_run_changes_nothing(self, client, cli_seed):
        output = self._run("--dry-run")
        assert "[dry-run]" in output
        db.session.expire_all()
        teacher = Users.query.filter_by(username="pa_teacher").first()
        assert "PaGroupA" not in {g.groupname for g in teacher.member_of_groups}

    def test_sync_adds_missing_memberships(self, client, cli_seed):
        output = self._run()
        db.session.expire_all()

        # teacher gets the membership, category untouched
        teacher = Users.query.filter_by(username="pa_teacher").first()
        assert "PaGroupA" in {g.groupname for g in teacher.member_of_groups}
        assert teacher.category == "teacher"

        # plain user gets membership; no promotion without --promote
        plain = Users.query.filter_by(username="pa_cli").first()
        assert "PaGroupA" in {g.groupname for g in plain.member_of_groups}
        assert plain.category == "user"

        # already-member user is not duplicated and not reported
        member = Users.query.filter_by(username="pa_member").first()
        assert [g.groupname for g in member.member_of_groups].count("PaGroupB") == 1
        assert "pa_member" not in output

        # soft-deleted users are skipped
        deleted = Users.query.filter_by(username="pa_deleted").first()
        assert "PaGroupA" not in {g.groupname for g in deleted.member_of_groups}

    def test_sync_is_idempotent(self, client, cli_seed):
        output = self._run()
        assert "0 user(s) updated" in output

    def test_promote_flag(self, client, cli_seed):
        # give the plain user a fresh membership opportunity
        group_b = Groups.query.filter_by(groupname="PaGroupB").first()
        group_b.set_approved_users(group_b.approved_users_list + ["pa-cli@test.local"])
        db.session.commit()

        output = self._run("--promote")
        assert "[promoted to student]" in output
        db.session.expire_all()
        plain = Users.query.filter_by(username="pa_cli").first()
        assert "PaGroupB" in {g.groupname for g in plain.member_of_groups}
        assert plain.category == "student"
