# -*- encoding: utf-8 -*-
from flask import render_template, request, jsonify, current_app
from flask_login import current_user, login_required
from apps.controllers.clabernetes import C9sController
import yaml
from apps import db
from apps.clabs.models import Clab, ClabInstance
from apps.authentication.models import Users
import json
import datetime as dt


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

# -------- Serialization helpers (DB → UI/API) --------
def _row_from_db(ci: ClabInstance) -> dict:
    """Linha para /running e /api/list."""
    owner = getattr(ci, "owner", None)
    clab = getattr(ci, "clab", None)
    created = (ci.created_at or dt.datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "lab_instance_id": ci.id,
        "user": getattr(owner, "username", None) or getattr(owner, "email", None) or "anonymous",
        "title": getattr(clab, "title", None) or "Unnamed",
        "created": created,
    }

def _detail_from_db(ci: ClabInstance) -> dict:
    """Detalhe para /open/<id>."""
    owner = getattr(ci, "owner", None)
    clab = getattr(ci, "clab", None)

    created = (ci.created_at or dt.datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    try:
        resources = json.loads(ci._clab_resources or "[]")
        if not isinstance(resources, list):
            resources = []
    except Exception:
        resources = []

    return {
        "lab_instance_id": ci.id,
        "user": getattr(owner, "username", None) or getattr(owner, "email", None) or "anonymous",
        "title": getattr(clab, "title", None) or "Unnamed",
        "created": created,
        "namespace": ci.namespace_effective or getattr(clab, "namespace_default", None) or "--",
        "resources": resources,
        "name": getattr(clab, "title", None) or "Unnamed",
        "status": "Running",
    }

# ----------------- Views -----------------
@blueprint.route("/running")
@login_required
def running():
    """List running labs. Admin/teacher sees all; users see only their labs."""
    q = ClabInstance.query.filter_by(is_deleted=False)
    if not _is_admin_or_teacher():
        q = q.filter_by(owner_user_id=current_user.id)
    cis = q.order_by(ClabInstance.created_at.desc()).all()
    items = [_row_from_db(ci) for ci in cis]
    return render_template("clabs/running.html", labs=items)

@blueprint.route("/open/<clab_id>")
@login_required
def open_clab(clab_id):
    ci = ClabInstance.query.get(clab_id)
    if not ci or ci.is_deleted:
        return render_template("pages/error.html", title="Open CLab", msg="CLab not found")
    if (not _is_admin_or_teacher()) and (ci.owner_user_id != current_user.id):
        return render_template("pages/error.html", title="Open CLab", msg="Forbidden"), 403

    lab = _detail_from_db(ci)
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

    # 1) usa o controller PoC para validar YAML e montar o "detail" compatível
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

    # 2) persistência no DB (MVP)
    clab_title = (detail.get("title") or name or "Unnamed").strip() or "Unnamed"
    clab_ns = namespace or None

    # encontra ou cria o template Clab pelo par (namespace_default, title)
    existing = Clab.query.filter_by(namespace_default=clab_ns, title=clab_title).first()
    if existing:
        clab = existing
    else:
        import uuid
        clab = Clab(
            id=str(uuid.uuid4()),
            title=clab_title,
            description=None,
            extended_desc=None,
            clab_guide_md=None,
            clab_guide_html=None,
            yaml_manifest=yaml_manifest or "",
            namespace_default=clab_ns,
        )
        db.session.add(clab)
        db.session.flush() 

    # cria a instância com o mesmo ID que o PoC gerou
    inst_id = detail.get("lab_instance_id")
    if not inst_id:
        inst_id = f"clab-{int(dt.datetime.utcnow().timestamp())}"

    token = f"tok_{inst_id}" 
    resources = json.dumps(detail.get("resources", []))

    ci = ClabInstance(
        id=inst_id,
        token=token,
        owner_user_id=current_user.id,
        clab_id=clab.id,
        is_deleted=False,
        expiration_ts=None,
        finish_reason=None,
        _clab_resources=resources,
        namespace_effective=detail.get("namespace") or clab_ns,
    )
    db.session.add(ci)
    db.session.commit()

    # 3) resposta compatível com a UI (mantida)
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
    pruned = 0

    q = ClabInstance.query.filter_by(is_deleted=False)
    if not _is_admin_or_teacher():
        q = q.filter_by(owner_user_id=current_user.id)
    cis = q.order_by(ClabInstance.created_at.desc()).all()
    items = [_row_from_db(ci) for ci in cis]
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
    ci = ClabInstance.query.get(clab_id)
    if not ci or ci.is_deleted:
        return jsonify({"ok": False, "error": "CLab not found"}), 404

    if (not _is_admin_or_teacher()) and (ci.owner_user_id != current_user.id):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    # tentativa de teardown no PoC (idempotente)
    try:
        ctrl().delete(clab_id)
    except Exception:
        current_app.logger.exception("PoC teardown failed (continuing)")

    ci.is_deleted = True
    ci.finish_reason = ci.finish_reason or "user_deleted" if not _is_admin_or_teacher() else "admin_cleanup"
    db.session.commit()
    return jsonify({"ok": True})

