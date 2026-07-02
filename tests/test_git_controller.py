"""Pytest suite for apps/controllers/git.py (GitController).

Covers update_repo (refresh short-circuit, clone, pull, and both error
branches), list_files, and get_file. GitPython is mocked via monkeypatch on
apps.controllers.git.git; the filesystem helpers use a real tmp dir. Runs inside
an app context so current_app.logger works in the error paths.

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_git_controller.py -v
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

import importlib  # noqa: E402

from run import app as flask_app  # noqa: E402

# NB: `apps.controllers.git` the *name* is a lazy proxy in the package
# namespace; import the real submodule so we can patch GitPython on it.
git_module = importlib.import_module("apps.controllers.git")
GitController = git_module.GitController

flask_app.config["TESTING"] = True


# --- fixtures ----------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _cleanup_temp_data_dir():
    yield
    import shutil

    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _app_context():
    with flask_app.app_context():
        yield


@pytest.fixture()
def ctrl():
    return GitController()


# --- update_repo --------------------------------------------------------
class TestUpdateRepo:
    def test_refresh_interval_short_circuits(self, ctrl, monkeypatch, tmp_path):
        calls = {"clone": 0}

        def fake_clone(*a, **k):
            calls["clone"] += 1

        monkeypatch.setattr(git_module.git.Repo, "clone_from", staticmethod(fake_clone))
        target = str(tmp_path / "repo")

        assert ctrl.update_repo("http://x", target, refresh_interval=9999) is True
        # second call within the interval must not clone again
        assert ctrl.update_repo("http://x", target, refresh_interval=9999) is True
        assert calls["clone"] == 1

    def test_clone_when_missing(self, ctrl, monkeypatch, tmp_path):
        called = {}

        def fake_clone(url, dest, **k):
            called["url"] = url

        monkeypatch.setattr(git_module.git.Repo, "clone_from", staticmethod(fake_clone))
        target = str(tmp_path / "newrepo")
        assert ctrl.update_repo("http://example/repo.git", target, refresh_interval=0) is True
        assert called["url"] == "http://example/repo.git"

    def test_pull_when_present(self, ctrl, monkeypatch, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        pulled = {"n": 0}

        class FakeOrigin:
            def pull(self, **k):
                pulled["n"] += 1

        class FakeRepo:
            def __init__(self, path):
                self.remotes = types.SimpleNamespace(origin=FakeOrigin())

        monkeypatch.setattr(git_module.git, "Repo", FakeRepo)
        assert ctrl.update_repo("http://x", str(target), refresh_interval=0) is True
        assert pulled["n"] == 1

    def test_git_command_error_returns_false(self, ctrl, monkeypatch, tmp_path):
        def boom(*a, **k):
            raise git_module.git.GitCommandError("clone", "failed")

        monkeypatch.setattr(git_module.git.Repo, "clone_from", staticmethod(boom))
        target = str(tmp_path / "willfail")
        assert ctrl.update_repo("http://x", target, refresh_interval=0) is False

    def test_generic_error_returns_false(self, ctrl, monkeypatch, tmp_path):
        def boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(git_module.git.Repo, "clone_from", staticmethod(boom))
        target = str(tmp_path / "willfail2")
        assert ctrl.update_repo("http://x", target, refresh_interval=0) is False


# --- list_files ---------------------------------------------------------
class TestListFiles:
    def test_lists_matching_files(self, ctrl, tmp_path):
        (tmp_path / "a.yaml").write_text("a")
        (tmp_path / "b.yaml").write_text("b")
        (tmp_path / "c.txt").write_text("c")
        result = ctrl.list_files(str(tmp_path), pattern="*.yaml", recursive=False)
        assert sorted(result) == ["a.yaml", "b.yaml"]


# --- get_file -----------------------------------------------------------
class TestGetFile:
    def test_missing_file(self, ctrl, tmp_path):
        status, result = ctrl.get_file(str(tmp_path), "nope.yaml")
        assert status is False
        assert result == "Template not found"

    def test_existing_file(self, ctrl, tmp_path):
        (tmp_path / "t.yaml").write_text("hello: world")
        status, result = ctrl.get_file(str(tmp_path), "t.yaml")
        assert status is True
        assert result == "hello: world"

    def test_read_error(self, ctrl, tmp_path, monkeypatch):
        (tmp_path / "t.yaml").write_text("x")

        def boom(self, *a, **k):
            raise OSError("disk gone")

        monkeypatch.setattr(git_module.Path, "read_text", boom)
        status, result = ctrl.get_file(str(tmp_path), "t.yaml")
        assert status is False
        assert "Failed to read template file" in result
