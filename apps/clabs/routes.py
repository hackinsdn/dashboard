# -*- encoding: utf-8 -*-
import os
from flask import render_template, request, jsonify, current_app
from flask_login import current_user
from datetime import datetime, timedelta
from . import blueprint

from apps.controllers.clabernetes import C9sController
import yaml  # agora é dependência real (requirements)

# bootstrap lazy do controller (singleton por processo)
_CLABS_CTRL = None
def ctrl() -> C9sController:
    global _CLABS_CTRL
    if _CLABS_CTRL is None:
        cfg = current_app.config
        _CLABS_CTRL = C9sController(
            state_path=cfg.get("CLABS_STATE_PATH"),
            expire_days=cfg.get("CLABS_EXPIRE_DAYS", 0),
            upload_max_files=cfg.get("CLABS_UPLOAD_MAX_FILES", 200),
        )
    return _CLABS_CTRL


def _norm_lab_row(c: dict) -> dict:
    """Normaliza um item de CLABS para exibição na tabela."""
    lab_id  = c.get("id") or c.get("lab_instance_id") or c.get("lab_id")
    user    = c.get("owner") or c.get("user") or "--"
    title   = c.get("name")  or c.get("title") or "--"
    created = c.get("created_at") or c.get("created") or "--"
    return {
        "lab_instance_id": lab_id,
        "user": user,
        "title": title,
        "created": created,
    }

@blueprint.route("/running")
def running():
    owner = getattr(current_user, "username", None) or getattr(current_user, "email", None)
    labs = ctrl().list(user=owner)
    return render_template("clabs/running.html", labs=labs)

@blueprint.route("/open/<clab_id>")
def open_clab(clab_id):
    lab = ctrl().get(clab_id)
    if not lab:
        return render_template("pages/error.html", title="Open CLab", msg="CLab not found")
    return render_template("clabs/open.html", lab=lab)


def _resources_from_clab_yaml(yaml_text: str):
    """
    Constrói a lista de 'resources' (nós) a partir de um YAML ContainerLab.
    Se PyYAML não estiver disponível ou o YAML estiver inválido, retorna [].
    """
    if not yaml or not yaml_text:
        return []

    try:
        data = yaml.safe_load(yaml_text)
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    topo = data.get("topology") or {}
    nodes = topo.get("nodes") or {}
    if not isinstance(nodes, dict):
        return []

    resources = []
    for name, cfg in nodes.items():
        kind = "node"
        ready = "Running"  
        pod_ip = None
        node_name = None
        links = [{"label": "Console", "href": "#"}] 
        services = []

        if isinstance(cfg, dict):
            pod_ip = cfg.get("mgmt_ipv4")
            node_name = cfg.get("kind") or cfg.get("image") or "--"

        resources.append({
            "kind": kind,
            "name": str(name),
            "ready": ready,
            "links": links,
            "services": services,
            "age": "--",
            "node_name": node_name or "--",
            "pod_ip": pod_ip or "--",
        })

    return resources

@blueprint.route("/create", methods=["GET", "POST"])
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
        return jsonify({"ok": False, "result": f"Too many files: {files_count} (max {max_files})"}), 400

    owner = getattr(current_user, "username", None) or getattr(current_user, "email", None) or "anonymous"

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
    except Exception as e:
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
def api_list():
    removed = ctrl().prune()  # respeita CLABS_EXPIRE_DAYS
    items = ctrl().list()
    return jsonify({"items": items, "pruned": removed})

@blueprint.route("/api/clear", methods=["POST"])
def api_clear_all():
    n = ctrl().clear()
    return jsonify({"ok": True, "removed": n})

@blueprint.route("/api/delete/<clab_id>", methods=["DELETE", "POST"])
def api_delete(clab_id):
    lab = ctrl().get(clab_id)
    if not lab:
        return jsonify({"ok": False, "error": "CLab not found"}), 404

    owner = getattr(current_user, "username", None) or getattr(current_user, "email", None)
    if lab.get("user") not in (owner, "anonymous"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    ok = ctrl().delete(clab_id)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "CLab not found"}), 404

