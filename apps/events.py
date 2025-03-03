import sys
import pty
import os
import signal
import subprocess
import select
import struct
import fcntl
import termios
import time

from flask import request, current_app
from flask_socketio import emit, disconnect
from apps import socketio
from apps.controllers import k8s
from flask_login import login_required, current_user

xterm_clients = {}


def check_authorization(username, pod, container):
    return True


def set_winsize(fd, row, col, xpix=0, ypix=0):
    winsize = struct.pack("HHHH", row, col, xpix, ypix)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def read_and_forward_pty_output(session_id, fd):
    max_read_bytes = 1024 * 20
    while True:
        socketio.sleep(0.01)
        if not fd:
            continue
        (data_ready, _, data_exc) = select.select([fd], [], [fd], 0)
        if not data_ready:
            continue
        if data_exc:
            break
        try:
            output = os.read(fd, max_read_bytes).decode(
                errors="ignore"
            )
        except Exception as exc:
            break
        socketio.emit("pty-output", {"output": output}, namespace="/pty", to=session_id)
    socketio.emit("server-disconnected", namespace="/pty", to=session_id)


@socketio.on("pty-input", namespace="/pty")
@login_required
def pty_input(data):
    """write to the child pty."""
    global xterm_clients
    session_id = request.sid
    if not session_id or session_id not in xterm_clients:
        current_app.logger.info(f"Host not connected {session_id=}")
        return False
    (fd, _) = xterm_clients[session_id]
    if fd:
        os.write(fd, data["input"].encode())


@socketio.on("connect", namespace="/pty")
@login_required
def pty_connect(auth):
    global xterm_clients
    """new client connected."""
    host = request.args.get("host")
    try:
        kind, pod, container = host.split("/")
    except:
        current_app.logger.error(f"Invalid host trying to open xterm {host=} user={current_user.username}")
        return False
    session_id = request.sid
    if not check_authorization(current_user.username, pod, container):
        current_app.logger.info(f"socketio connnect request for unknown {request.args=}")
        return False
    if session_id in xterm_clients:
        current_app.logger.info(f"session already connected")
        return False
    current_app.logger.info(f"connecting to {pod=} {container=} {session_id=}")
    # create child process attached to a pty we can read from and write to
    (child_pid, fd) = pty.fork()
    if child_pid == 0:
        # this is the child process fork.
        # change default env vars to avoid massive logs when DEBUG is enabled
        myenv = dict(os.environ)
        myenv.update({"DEBUG": ""})
        subprocess.run(['kubectl', 'exec', '-it', f"{kind}/{pod}", '--container', container, '--', 'sh', '-c', 'if [ -x /bin/bash ]; then exec /bin/bash; else exec /bin/sh; fi'], env=myenv)
        os._exit(os.EX_OK)
    else:
        # this is the parent process fork.
        # store child fd and pid
        xterm_clients[session_id] = (fd, child_pid)
        set_winsize(fd, 50, 50)
        socketio.start_background_task(read_and_forward_pty_output, session_id, fd)
        current_app.logger.info(f"client added to xterm_clients {session_id=} {fd=} {child_pid=}")


@socketio.on("resize", namespace="/pty")
@login_required
def resize(data):
    """resize window lenght"""
    global xterm_clients
    session_id = request.sid
    if not session_id or session_id not in xterm_clients:
        current_app.logger.info(f"Host not connected {session_id=}")
        return False
    (fd, _) = xterm_clients[session_id]
    if fd:
        set_winsize(fd, data["dims"]["rows"], data["dims"]["cols"])


@socketio.on("disconnect", namespace="/pty")
@login_required
def pty_disconnect():
    """client disconnected."""
    global xterm_clients
    session_id = request.sid
    if not session_id or session_id not in xterm_clients:
        current_app.logger.info(f"Host not connected {session_id=} {xterm_clients=}")
        return False
    (_, pid) = xterm_clients[session_id]
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        print(f"Error terminating xterm child process for {session_id}: {exc}")
    print(f"Client disconnected {session_id}")
    xterm_clients.pop(session_id, None)
