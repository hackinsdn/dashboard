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

from apps.controllers import k8s

xterm_clients = {}


def check_authorization(user, lab_id, kind, pod, container):
    """Verify ``user`` may open a shell into ``pod`` of lab instance ``lab_id``.

    The /pty websocket is a separate connection from the xterm HTTP page, so it
    must re-authorize on its own rather than trust that the page was served.
    Access is denied when the lab instance is unknown/finished, the user is not
    its owner (nor an admin or a privileged member of one of the lab's groups),
    the pod no longer exists, or the pod does not belong to this instance. The
    existence check stops the handler from dialing a pod that is gone -- e.g. a
    stale browser tab left open on a lab that has since been torn down, whose
    reconnect attempts would otherwise hit a 404 on every exec.
    """
    # imported lazily to avoid a circular import at module load (apps -> events)
    from apps import db
    from apps.home.models import LabInstances, Labs

    if not lab_id:
        return False
    lab_instance = db.session.get(LabInstances, lab_id)
    if not lab_instance or lab_instance.is_deleted:
        current_app.logger.info(
            f"xterm authz denied: lab instance not found/finished {lab_id=} user={user.username}"
        )
        return False

    # owner, admin, or a privileged (owner/assistant) member of one of the lab's
    # allowed groups -- mirrors view_lab_instance in apps/home/routes.py
    authorized = lab_instance.user_id == user.id or user.category == "admin"
    if not authorized:
        lab = db.session.get(Labs, lab_instance.lab_id)
        if lab:
            privileged_group_ids = user.privileged_group_ids
            authorized = any(g.id in privileged_group_ids for g in lab.allowed_groups)
    if not authorized:
        current_app.logger.info(
            f"xterm authz denied: user not authorized {lab_id=} user={user.username}"
        )
        return False

    # the pod must still exist (a stale tab on a torn-down lab is rejected here,
    # before any exec is attempted -- a 404 read is not a failover trigger, so it
    # no longer cycles the kubeconfigs). read_namespaced_pod raises on NotFound.
    try:
        pod_obj = k8s.get_pod_by_name({"name": pod})
    except Exception as exc:
        current_app.logger.info(
            f"xterm authz denied: {pod=} not found for {lab_id=} user={user.username}: {exc}"
        )
        return False

    # and it must belong to THIS lab instance, so an authorized user of one lab
    # cannot exec into another lab's pod by naming it. Pods do not carry the
    # user_uid/lab_id labels (those live on the owning Deployment/Topology), but
    # the pod's "app"/"clabernetes/name" label matches a resource name recorded
    # for the instance (the Deployment name for regular labs, the Topology name
    # for clab), so bind on that without any extra API call.
    labels = (pod_obj.get("metadata") or {}).get("labels") or {}
    pod_keys = {labels.get("app"), labels.get("clabernetes/name")}
    resource_names = {res.get("name") for res in lab_instance.k8s_resources}
    if not (pod_keys & resource_names):
        current_app.logger.info(
            f"xterm authz denied: {pod=} does not belong to {lab_id=} user={user.username}"
        )
        return False
    return True


def set_winsize(fd, row, col, xpix=0, ypix=0):
    winsize = struct.pack("HHHH", row, col, xpix, ypix)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def resize_terminal(client_stream, rows, cols):
    client_stream.write_channel(4, '{"Width":%d,"Height":%d}' % (cols, rows))


def read_and_forward_pty_output(session_id, fd, pid):
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
            socketio.emit("pty-output", {"output": output}, namespace="/pty", to=session_id)
        except Exception as exc:
            break
    start_time = time.time()
    while time.time() - start_time < 5:
        pid_returned, status = os.waitpid(pid, os.WNOHANG)
        if pid_returned == pid:
            status = os.waitstatus_to_exitcode(status)
            break
    else:
        try:
            os.kill(pid, 9)
        except:
            pass
        status = 255
    socketio.emit("server-disconnected", {"returncode": status}, namespace="/pty", to=session_id)


def read_and_forward_k8s_stream_output(session_id, client_stream):
    max_read_bytes = 1024 * 20
    while client_stream.is_open():
        socketio.sleep(0.01)
        try:
            data = client_stream.read_stdout(max_read_bytes)
        except Exception as exc:
            break
        if client_stream.is_open():
            if len(data or "") > 0:
                socketio.emit("pty-output", {"output": data}, namespace="/pty", to=session_id)
        else:
            break
    # Forward the process exit code so the client can auto-close on a clean
    # exit (0) and show it otherwise. WSClient.returncode parses the error
    # channel, which can raise/return None if no status was received; fall
    # back to None so the client just reports a plain disconnect.
    try:
        returncode = client_stream.returncode
    except Exception:
        returncode = None
    socketio.emit("server-disconnected", {"returncode": returncode}, namespace="/pty", to=session_id)


@socketio.on("pty-input", namespace="/pty")
@login_required
def pty_input(data):
    """write to the child pty."""
    global xterm_clients
    session_id = request.sid
    if not session_id or session_id not in xterm_clients:
        current_app.logger.info(f"Host not connected {session_id=}")
        return False
    client_stream = xterm_clients[session_id]
    if client_stream.is_open():
        client_stream.write_stdin(data["input"].encode())


@socketio.on("connect", namespace="/pty")
@login_required
def pty_connect(auth):
    global xterm_clients
    """new client connected."""
    host = request.args.get("host")
    lab_id = request.args.get("lab_id")
    try:
        kind, pod, container = host.split("/")
    except:
        current_app.logger.error(f"Invalid host trying to open xterm {host=} user={current_user.username}")
        return False
    session_id = request.sid
    if not check_authorization(current_user, lab_id, kind, pod, container):
        current_app.logger.info(f"xterm connect rejected {request.args=} user={current_user.username}")
        return False
    if session_id in xterm_clients:
        current_app.logger.info(f"session already connected")
        return False
    current_app.logger.info(f"connecting to {pod=} {container=} {session_id=}")
    start_script = 'if [ -x /bin/bash ]; then exec /bin/bash; else exec /bin/sh; fi'
    if kind == "clab":
        kind = "pod"
        start_script = f"ssh {container}"
    try:
        client_stream = k8s.get_pod_exec_stream(pod, container, start_script)
    except Exception as exc:
        current_app.logger.error(f"Failed to open exec stream to {pod=} {container=}: {exc}")
        return False
    xterm_clients[session_id] = client_stream
    socketio.start_background_task(read_and_forward_k8s_stream_output, session_id, client_stream)
    current_app.logger.info(f"client added to xterm_clients {session_id=}")


@socketio.on("resize", namespace="/pty")
@login_required
def resize(data):
    """resize window lenght"""
    global xterm_clients
    session_id = request.sid
    if not session_id or session_id not in xterm_clients:
        current_app.logger.info(f"Host not connected {session_id=}")
        return False
    client_stream = xterm_clients[session_id]
    resize_terminal(client_stream, data["dims"]["rows"], data["dims"]["cols"])


@socketio.on("disconnect", namespace="/pty")
@login_required
def pty_disconnect(message=None):
    """client disconnected."""
    global xterm_clients
    session_id = request.sid
    if not session_id or session_id not in xterm_clients:
        current_app.logger.warning(f"Host not connected {session_id=} {xterm_clients=}")
        return False
    current_app.logger.info(f"pty_disconnect client={request.sid} message={message}")
    client_stream = xterm_clients[session_id]
    try:
        client_stream.close()
    except Exception as exc:
        current_app.logger.error(f"Error terminating xterm child process for {session_id}: {exc}")
    current_app.logger.info(f"Client disconnected {session_id}")
    xterm_clients.pop(session_id, None)
