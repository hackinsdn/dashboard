# Implementation plan: unit tests for `apps/events.py`

## Context

`apps/events.py` implements the xterm/PTY terminal streaming over Socket.IO: a
few standalone helpers plus four `@socketio.on(..., namespace="/pty")` event
handlers (`pty_connect`, `pty_input`, `resize`, `pty_disconnect`) and two
forwarding loops. It has no coverage.

The app runs Socket.IO with `async_mode='gevent'` and the handlers do PTY/stream
I/O and spawn background tasks - so driving them through a real
`socketio.test_client` would be slow and flaky (greenlets, blocking read loops).
The stable approach is to **call the functions directly** with their
dependencies mocked, using plain Flask app/request contexts. This needs no DB.

### Key techniques

- Import the module as `import apps.events as events` (it is already loaded by
  `create_app`, so this returns the cached module with handlers registered).
- **Bypass `@login_required`** on the handlers by setting
  `flask_app.config["LOGIN_DISABLED"] = True`, and give the handlers a user by
  monkeypatching the module global `events.current_user` to a
  `SimpleNamespace(username="tester")`.
- **`request.sid`**: handlers read `request.sid` (set by Flask-SocketIO at
  runtime). In tests, enter `flask_app.test_request_context(<path>)` and assign
  `request.sid = "sid1"` (and use the query string for `pty_connect`'s `host`).
- **Mock the boundaries** by monkeypatching module globals:
  `events.k8s.get_pod_exec_stream`, `events.socketio.emit`,
  `events.socketio.sleep` (-> no-op, so the loops don't actually sleep),
  `events.socketio.start_background_task` (-> capture, so no greenlet spawns),
  and `events.os` / `events.select` for the PTY loop.
- The module-level `events.xterm_clients` dict is cleared before each test (an
  autouse fixture) so sessions don't leak between tests.

## New file: `tests/test_events.py`

Harness = clabernetes stub + `from run import app` (for the socketio/k8s wiring
and `current_app`). No database. Most handler tests run inside a
`test_request_context` with `request.sid` set and `LOGIN_DISABLED` on.

### TestHelpers (direct, no socket context)
- `check_authorization(...)` -> always True.
- `resize_terminal`: a fake stream -> asserts
  `stream.write_channel(4, '{"Width":80,"Height":24}')`.
- `set_winsize`: open a real pty with `os.openpty()`, call
  `set_winsize(slave_fd, 24, 80)` -> no error (optionally read back the winsize
  via `termios`); close the fds.
- `read_and_forward_k8s_stream_output`: a fake `client_stream` whose `is_open()`
  yields `[True, True, False]` and `read_stdout` returns `"hello"`; with
  `socketio.emit` captured and `socketio.sleep` no-op -> emits one
  `pty-output {"output": "hello"}` then a final `server-disconnected`.
- `read_and_forward_pty_output`: monkeypatch `events.select.select`
  (`[([fd],[],[]), ([],[],[fd])]` -> one read then an exception-break),
  `events.os.read` (-> `b"hello"`), `events.os.waitpid` (-> `(pid, 0)`),
  `events.os.waitstatus_to_exitcode` (-> 0), `socketio.emit`/`sleep` -> emits the
  output chunk and then `server-disconnected` with `returncode 0`.

### TestPtyConnect (`test_request_context` + query string)
- invalid `host` (missing / not `a/b/c`) -> returns `False`, nothing added to
  `xterm_clients`.
- happy path `host="pod/mypod/mycont"`: `k8s.get_pod_exec_stream` mocked to a
  fake stream, `start_background_task` captured -> `xterm_clients[sid]` is the
  stream and the background task was scheduled with it.
- `kind="clab"`: `host="clab/mypod/mycont"` -> `get_pod_exec_stream` is called
  with `start_script == "ssh mycont"` (and kind coerced to pod).
- already-connected sid -> returns `False` (no overwrite).

### TestPtyInput / TestResize / TestPtyDisconnect
- **not connected** (sid absent from `xterm_clients`) -> each returns `False`.
- `pty_input` connected: fake stream `is_open() == True` -> `write_stdin` called
  with the encoded input.
- `resize` connected: -> `resize_terminal` reaches the stream's `write_channel`.
- `pty_disconnect` connected: -> `stream.close()` called and the sid removed
  from `xterm_clients`; and a variant where `close()` raises -> the error is
  swallowed/logged and the sid is still removed.

## Note (not a required fix)

`check_authorization` is a stub that always returns `True` (auth is effectively
open on the PTY namespace). That is existing behaviour; flagging it for
awareness, not changing it here.

## Files to create
- `tests/test_events.py`

No production changes expected.

## Verification
```
pip install -r requirements-dev.txt
pytest tests/test_events.py -v
pytest tests/ -v          # whole suite stays green
pytest tests/ --cov=apps.events --cov-report=term-missing
```
