"""Pytest suite for apps/utils.py.

Covers the pure helpers (utcnow, parse_lab_expiration, datetime_from_ts,
secure_filename, format_duration, list_files, remove_empty_folders) and the
DB-backed helpers (check_pre_approved, update_running_labs_stats,
update_category_stats, update_stats_lab_instances_answers).

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_utils.py -v

The seeded usernames/groups are prefixed "ut" so they never collide with the
other test modules that share the same singleton app/database.
"""
import datetime
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
from apps import db, cache  # noqa: E402
from apps import utils  # noqa: E402
from apps.authentication.models import Users, Groups  # noqa: E402
from apps.home.models import Labs, LabCategories, LabInstances, LabAnswers  # noqa: E402

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


# ============================ pure helpers ============================
class TestPureHelpers:
    def test_utcnow_is_timezone_aware(self):
        now = utils.utcnow()
        assert now.tzinfo is not None
        assert abs((datetime.datetime.now(datetime.timezone.utc) - now).total_seconds()) < 5

    def test_parse_lab_expiration_never(self):
        assert utils.parse_lab_expiration("0") is None

    def test_parse_lab_expiration_hours(self):
        ts = utils.parse_lab_expiration("4")
        expected = utils.utcnow() + datetime.timedelta(hours=4)
        assert abs(ts - int(expected.timestamp())) < 5

    def test_datetime_from_ts_valid(self):
        result = utils.datetime_from_ts(0)
        assert result == "1970-01-01T00:00:00+00:00"

    def test_datetime_from_ts_invalid(self):
        assert utils.datetime_from_ts("not-a-timestamp") is None

    def test_secure_filename_cases(self):
        assert utils.secure_filename("My cool movie.mov") == "My_cool_movie.mov"
        assert utils.secure_filename("../../../etc/passwd") == "etc/passwd"
        assert utils.secure_filename("i contain cool \xfcml\xe4uts.txt") == "i_contain_cool_umlauts.txt"

    def test_format_duration(self):
        assert utils.format_duration(datetime.timedelta(0)) == "0s"
        assert utils.format_duration(datetime.timedelta(seconds=-5)) == "--"
        assert utils.format_duration(datetime.timedelta(minutes=2, seconds=3)) == "2m3s"
        assert utils.format_duration(datetime.timedelta(days=10)) == "10d"

    def test_list_files(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("b")
        (tmp_path / "ignore-me.txt").write_text("x")

        files = utils.list_files(str(tmp_path))
        assert any(f.endswith("a.txt") for f in files)
        assert any(f.endswith("b.txt") for f in files)

        filtered = utils.list_files(str(tmp_path), ignore_prefix="ignore")
        assert not any("ignore-me.txt" in f for f in filtered)

    def test_remove_empty_folders(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        full = tmp_path / "full"
        full.mkdir()
        (full / "keep.txt").write_text("keep")

        utils.remove_empty_folders(str(tmp_path))
        assert not empty.exists()
        assert full.exists()


# ============================ DB-backed helpers ============================
class TestCheckPreApproved:
    def test_non_user_category_is_ignored(self, app):
        student = Users(username="ut_student", password="p", email="ut_student@test.local", category="student")
        db.session.add(student)
        db.session.commit()
        assert utils.check_pre_approved(student) is None
        assert student.category == "student"

    def test_member_of_normal_group_is_promoted(self, app):
        user = Users(username="ut_member", password="p", email="ut_member@test.local", category="user")
        group = Groups(groupname="UtNormal", organization="ORG1")
        db.session.add_all([user, group])
        db.session.commit()
        group.members.append(user)
        db.session.commit()

        assert utils.check_pre_approved(user) is True
        assert user.category == "student"

    def test_system_only_group_does_not_promote(self, app):
        user = Users(username="ut_sysonly", password="p", email="ut_sysonly@test.local", category="user")
        sysgroup = Groups(groupname="UtSystem", organization="SYSTEM")
        db.session.add_all([user, sysgroup])
        db.session.commit()
        sysgroup.members.append(user)
        db.session.commit()

        # no non-SYSTEM group and not in any approved_users list -> not promoted
        assert not utils.check_pre_approved(user)
        assert user.category == "user"

    def test_approved_email_promotes_and_adds_to_group(self, app):
        user = Users(username="ut_approved", password="p", email="ut_approved@test.local", category="user")
        group = Groups(groupname="UtApproved", organization="ORG2")
        db.session.add_all([user, group])
        db.session.commit()
        group.set_approved_users(["ut_approved@test.local"])
        db.session.commit()

        assert utils.check_pre_approved(user) is True
        assert user.category == "student"
        assert group.is_member(user.id)


class TestStats:
    def test_update_running_labs_stats_sets_g_and_cache(self, app):
        user = Users(username="ut_runstats", password="p", email="ut_runstats@test.local", category="student")
        lab = Labs(title="Ut Stats Lab", description="x")
        db.session.add_all([user, lab])
        db.session.commit()
        inst = LabInstances(user_id=user.id, lab_id=lab.id, is_deleted=False)
        inst.k8s_resources = []
        db.session.add(inst)
        db.session.commit()

        cache.delete("running_labs-all")
        with flask_app.test_request_context("/"):
            from flask import g

            utils.update_running_labs_stats()
            assert g.running_labs >= 1
        assert cache.get("running_labs-all") is not None

    def test_update_category_stats(self, app):
        cat = LabCategories(category="UtCatStats", color_cls="dark")
        cat.updated_by = None
        lab = Labs(title="Ut Cat Lab", description="x")
        lab.categories.append(cat)
        db.session.add_all([cat, lab])
        db.session.commit()

        stats = utils.update_category_stats()
        assert "UtCatStats" in stats["category_names"]
        idx = stats["category_names"].index("UtCatStats")
        assert stats["usage_counts"][idx] >= 1

    def test_update_stats_lab_instances_answers_shape(self, app):
        user = Users(username="ut_answerstats", password="p", email="ut_answerstats@test.local", category="student")
        lab = Labs(title="Ut Answer Lab", description="x")
        db.session.add_all([user, lab])
        db.session.commit()
        finished = LabInstances(user_id=user.id, lab_id=lab.id, is_deleted=True, finish_reason="done")
        finished.k8s_resources = []
        # the stats window covers the *previous* 6 months (not the current one),
        # so place this ~2 months back to guarantee it lands in a bucket
        finished.created_at = utils.utcnow() - datetime.timedelta(days=60)
        db.session.add(finished)
        db.session.commit()

        stats = utils.update_stats_lab_instances_answers()
        assert len(stats["months"]) == 6
        assert len(stats["labs"]) == 6
        assert stats["total_labs"] >= 1
