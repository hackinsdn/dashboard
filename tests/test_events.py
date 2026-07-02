"""Pytest suite for apps/events.py (xterm/PTY Socket.IO handlers).

The app uses Socket.IO with gevent, and these handlers do PTY/stream I/O and
spawn background tasks - so instead of driving a real socketio test client
(slow/flaky), the functions are called directly with their boundaries mocked,
inside plain Flask app/request contexts. No database is used.

Techniques:
- @login_required on the handlers is bypassed with LOGIN_DISABLED=True plus a
  monkeypatched module-global `events.current_user`.
- request.sid is set inside a test_request_context; pty_connect's host comes from
  the query string.
- os/select/socketio/k8s are monkeypatched on the module so nothing real runs.

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_events.py -v
"""
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock

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
import apps.events as events  # noqa: E402
from flask import request  # noqa: E402

flask_app.config["TESTING"] = True

DEFAULT_START = 'if [ -x /bin/bash ]; then exec /bin/bash; else exec /bin/sh; fi'


# --- fixtures ----------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _cleanup_temp_data_dir():
    yield
    import shutil

    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clear_clients():
    events.xterm_clients.clear()
    yield
    events.xterm_clients.clear()


@pytest.fixture()
def logged_in(monkeypatch):
    """Neutralise @login_required and give the handlers a current_user."""
    monkeypatch.setitem(flask_app.config, "LOGIN_DISABLED", True)
    monkeypatch.setattr(events, "current_user", types.SimpleNamespace(username="tester", is_authenticated=True))


@pytest.fixture()
def captured_emits(monkeypatch):
    emits = []
    monkeypatch.setattr(events.socketio, "emit", lambda *a, **k: emits.append((a, k)))
    monkeypatch.setattr(events.socketio, "sleep", lambda *a, **k: None)
    return emits


# --- standalone helpers -------------------------------------------------
class TestHelpers:
    def test_check_authorization(self):
        assert events.check_authorization("user", "pod", "container") is True

    def test_resize_terminal(self):
        stream = MagicMock()
        events.resize_terminal(stream, 24, 80)
        stream.write_channel.assert_called_once_with(4, '{"Width":80,"Height":24}')

    def test_set_winsize(self):
        import fcntl
        import struct
        import termios

        master, slave = os.openpty()
        try:
            events.set_winsize(slave, 24, 80)
            packed = fcntl.ioctl(slave, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
            rows, cols, _, _ = struct.unpack("HHHH", packed)
            assert (rows, cols) == (24, 80)
        finally:
            os.close(master)
            os.close(slave)

    def test_read_and_forward_k8s_stream_output(self, captured_emits):
        stream = MagicMock()
        stream.is_open.side_effect = [True, True, False]
        stream.read_stdout.return_value = "hello"

        events.read_and_forward_k8s_stream_output("sid1", stream)

        assert captured_emits[0][0][0] == "pty-output"
        assert captured_emits[0][0][1] == {"output": "hello"}
        assert captured_emits[-1][0][0] == "server-disconnected"

    def test_read_and_forward_pty_output(self, captured_emits, monkeypatch):
        fd, pid = 5, 4321
        fake_select = MagicMock(side_effect=[([fd], [], []), ([fd], [], [fd])])
        monkeypatch.setattr(events, "select", types.SimpleNamespace(select=fake_select))
        monkeypatch.setattr(events, "os", types.SimpleNamespace(
            read=lambda f, n: b"hello",
            waitpid=lambda p, opt: (pid, 0),
            waitstatus_to_exitcode=lambda s: 0,
            WNOHANG=os.WNOHANG,
        ))

        events.read_and_forward_pty_output("sid1", fd, pid)

        assert captured_emits[0][0][0] == "pty-output"
        assert captured_emits[0][0][1] == {"output": "hello"}
        assert captured_emits[-1][0][0] == "server-disconnected"
        assert captured_emits[-1][0][1] == {"returncode": 0}


# --- pty_connect --------------------------------------------------------
class TestPtyConnect:
    def test_invalid_host_is_rejected(self, logged_in):
        with flask_app.test_request_context("/"):
            request.sid = "sid1"
            assert events.pty_connect(None) is False
        assert events.xterm_clients == {}

    def test_happy_path_registers_session(self, logged_in, monkeypatch):
        fake_stream = MagicMock()
        get_stream = MagicMock(return_value=fake_stream)
        monkeypatch.setattr(events, "k8s", types.SimpleNamespace(get_pod_exec_stream=get_stream))
        bg = MagicMock()
        monkeypatch.setattr(events.socketio, "start_background_task", bg)

        with flask_app.test_request_context("/?host=pod/mypod/mycont"):
            request.sid = "sid1"
            events.pty_connect(None)

        assert events.xterm_clients["sid1"] is fake_stream
        get_stream.assert_called_once_with("mypod", "mycont", DEFAULT_START)
        bg.assert_called_once()

    def test_clab_kind_uses_ssh_start_script(self, logged_in, monkeypatch):
        get_stream = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(events, "k8s", types.SimpleNamespace(get_pod_exec_stream=get_stream))
        monkeypatch.setattr(events.socketio, "start_background_task", MagicMock())

        with flask_app.test_request_context("/?host=clab/mypod/mycont"):
            request.sid = "sid1"
            events.pty_connect(None)

        get_stream.assert_called_once_with("mypod", "mycont", "ssh mycont")

    def test_already_connected_is_rejected(self, logged_in, monkeypatch):
        sentinel = object()
        events.xterm_clients["sid1"] = sentinel
        monkeypatch.setattr(events, "k8s", types.SimpleNamespace(get_pod_exec_stream=MagicMock()))

        with flask_app.test_request_context("/?host=pod/mypod/mycont"):
            request.sid = "sid1"
            assert events.pty_connect(None) is False

        assert events.xterm_clients["sid1"] is sentinel  # not overwritten


# --- pty_input ----------------------------------------------------------
class TestPtyInput:
    def test_not_connected_returns_false(self, logged_in):
        with flask_app.test_request_context("/"):
            request.sid = "sid1"
            assert events.pty_input({"input": "ls\n"}) is False

    def test_connected_writes_stdin(self, logged_in):
        stream = MagicMock()
        stream.is_open.return_value = True
        events.xterm_clients["sid1"] = stream

        with flask_app.test_request_context("/"):
            request.sid = "sid1"
            events.pty_input({"input": "ls\n"})

        stream.write_stdin.assert_called_once_with(b"ls\n")


# --- resize -------------------------------------------------------------
class TestResize:
    def test_not_connected_returns_false(self, logged_in):
        with flask_app.test_request_context("/"):
            request.sid = "sid1"
            assert events.resize({"dims": {"rows": 24, "cols": 80}}) is False

    def test_connected_resizes(self, logged_in):
        stream = MagicMock()
        events.xterm_clients["sid1"] = stream

        with flask_app.test_request_context("/"):
            request.sid = "sid1"
            events.resize({"dims": {"rows": 24, "cols": 80}})

        stream.write_channel.assert_called_once_with(4, '{"Width":80,"Height":24}')


# --- pty_disconnect -----------------------------------------------------
class TestPtyDisconnect:
    def test_not_connected_returns_false(self, logged_in):
        with flask_app.test_request_context("/"):
            request.sid = "sid1"
            assert events.pty_disconnect() is False

    def test_connected_closes_and_removes(self, logged_in):
        stream = MagicMock()
        events.xterm_clients["sid1"] = stream

        with flask_app.test_request_context("/"):
            request.sid = "sid1"
            events.pty_disconnect()

        stream.close.assert_called_once()
        assert "sid1" not in events.xterm_clients

    def test_close_error_is_swallowed(self, logged_in):
        stream = MagicMock()
        stream.close.side_effect = RuntimeError("boom")
        events.xterm_clients["sid1"] = stream

        with flask_app.test_request_context("/"):
            request.sid = "sid1"
            events.pty_disconnect()

        assert "sid1" not in events.xterm_clients
