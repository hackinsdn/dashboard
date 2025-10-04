# -*- encoding: utf-8 -*-
from flask import render_template, request, jsonify, current_app
from flask_login import current_user, login_required
from apps.controllers.clabernetes import C9sController
import yaml

from . import blueprint

# -------- Controller singleton (lazy) --------
_CLABS_CTRL = None
def ctrl() -> C9sController:
    """Return a lazily-initialized controller instance."""
    global _CLABS_CTRL
    if _CLABS_CTRL is None:
        cfg = current_app.config
        _CLABS_CTRL = C9sController(
            expire_days=cfg.get("CLABS_EXPIRE_DAYS", 0),
            upload_max_files=cfg.get("CLABS_UPLOAD_MAX_FILES", 200),
        )
    return _CLABS_CTRL

# -------- Access helpers --------
def _is_admin_or_teacher() -> bool:
    cat = getattr(current_user, "category", "") or ""
    return cat in ("admin", "teacher")

def _owner_identity() -> str:
    return getattr(current_user, "username", None) or getattr(current_user, "email", None) or "anonymous"

# ----------------- Views -----------------
@blueprint.route("/running")
@login_required
def running():
    """List running labs. Admin/teacher sees all; users see only their labs."""
    if _is_admin_or_teacher():
        items = ctrl().list()
    else:
        items = ctrl().list(user=_owner_identity())
    return render_template("clabs/running.html", labs=items)

@blueprint.route("/open/<clab_id>")
@login_required
def open_clab(clab_id):
    lab = ctrl().get(clab_id)
    if not lab:
        return render_template("pages/error.html", title="Open CLab", msg="CLab not found")
    if (not _is_admin_or_teacher()) and (lab.get("user") != _owner_identity()):
        return render_template("pages/error.html", title="Open CLab", msg="Forbidden"), 403
    return render_template("clabs/open.html", lab=lab)

@blueprint.route("/create", methods=["GET", "POST"])
@login_required
def create():
    if request.method == "GET":
        return render_template("clabs/create.html")

    name = (request.form.get("clab_name") or "").strip()
    namespace = (request.form.get("clab_namespace") or "").strip()
    yaml_manifest = (request.form.get("clab_yaml") or "").strip()

    files = request.files.getlist("clab_files") or []
    files_meta = [{
        "name": f.filename,
        "mimetype": getattr(f, "mimetype", "") or "",
        "size": getattr(f, "content_length", None),
    } for f in files]
    files_count = len(files_meta)

    if not yaml_manifest and files_count == 0:
        return jsonify({"ok": False, "result": "Provide YAML and/or files"}), 400

    max_files = current_app.config.get("CLABS_UPLOAD_MAX_FILES", 200)
    if files_count > max_files:
        return jsonify({
            "ok": False,
            "result": f"Too many files: {files_count} (max {max_files})"
        }), 400

    owner = _owner_identity()

    try:
        detail = ctrl().create(
            name=name,
            namespace=namespace,
            yaml_manifest=yaml_manifest,
            files_meta=files_meta,
            owner=owner,
        )
    except yaml.YAMLError as e:
        return jsonify({"ok": False, "result": f"Invalid YAML: {e}"}), 400
    except Exception:
        current_app.logger.exception("Failed to create CLab")
        return jsonify({"ok": False, "result": "Internal error"}), 500

    nodes_count = len(detail.get("resources", []))
    return jsonify({
        "ok": True,
        "result": f"CLab '{detail.get('title','Unnamed')}' created.",
        "received": {
            "id": detail.get("lab_instance_id"),
            "namespace": detail.get("namespace") or "--",
            "has_yaml": bool(yaml_manifest),
            "files": files_count,
            "resources": nodes_count,
        }
    }), 201

@blueprint.route("/api/list", methods=["GET"])
@login_required
def api_list():
    """Return list of labs + number of pruned items (TTL)."""
    pruned = ctrl().prune()
    if _is_admin_or_teacher():
        items = ctrl().list()
    else:
        items = ctrl().list(user=_owner_identity())
    return jsonify({"items": items, "pruned": pruned})

@blueprint.route("/api/clear", methods=["POST"])
@login_required
def api_clear_all():
    """Admin/teacher-only: clear all labs from state."""
    if not _is_admin_or_teacher():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    n = ctrl().clear()
    return jsonify({"ok": True, "removed": n})

@blueprint.route("/api/delete/<clab_id>", methods=["DELETE", "POST"])
@login_required
def api_delete(clab_id):
    """Delete a single lab. Owners can delete their own; admin/teacher can delete any."""
    lab = ctrl().get(clab_id)
    if not lab:
        return jsonify({"ok": False, "error": "CLab not found"}), 404

    if (not _is_admin_or_teacher()) and (lab.get("user") != _owner_identity()):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    ok = ctrl().delete(clab_id)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "CLab not found"}), 404
