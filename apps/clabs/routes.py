# -*- encoding: utf-8 -*-
import os
from flask import render_template, request, jsonify, current_app
from flask_login import current_user
from datetime import datetime
from .store import load_state, save_state, prune_inplace, default_state_path
from . import blueprint
from .mock_data import CLABS, CLABS_DETAILS

# Tentamos usar PyYAML; se não estiver instalado, seguimos sem quebrar
try:
    import yaml  # PyYAML
except Exception:
    yaml = None

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
    labs = []
    for c in CLABS:
        lab_id  = c.get("id") or c.get("lab_instance_id") or c.get("lab_id")
        user    = c.get("owner") or c.get("user") or "--"
        title   = c.get("name")  or c.get("title") or "--"
        created = c.get("created_at") or c.get("created") or "--"

        # se não tiver id em nenhum formato, pula a linha
        if not lab_id:
            continue

        labs.append({
            "lab_instance_id": lab_id,
            "user": user,
            "title": title,
            "created": created,
        })

    return render_template("clabs/running.html", labs=labs)

@blueprint.route("/open/<clab_id>")
def open_clab(clab_id):
    lab = CLABS_DETAILS.get(clab_id)
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

    # --- POST (PoC) ---
    # Campos do formulário
    name = (request.form.get("clab_name") or "").strip()
    namespace = (request.form.get("clab_namespace") or "").strip()
    yaml_manifest = (request.form.get("clab_yaml") or "").strip()

    files = request.files.getlist("clab_files") or []
    files_meta = []
    for f in files:
        files_meta.append({
            "name": f.filename,                 
            "mimetype": getattr(f, "mimetype", "") or "",
            "size": getattr(f, "content_length", None),
        })
    files_count = len(files_meta)

    # Regras mínimas de envio: YAML ou pelo menos 1 arquivo
    if not yaml_manifest and files_count == 0:
        return jsonify({"ok": False, "result": "Provide YAML and/or files"}), 400

    # Limites de upload (quantidade) - PoC
    max_files = current_app.config.get("CLABS_UPLOAD_MAX_FILES", 200)
    if files_count > max_files:
        return jsonify({
            "ok": False,
            "result": f"Too many files: {files_count} (max {max_files})"
        }), 400

    # Validação de YAML no servidor (fallback/autoridade)
    # Se o usuário enviou YAML e houver PyYAML disponível, valide e falhe (400) se inválido.
    if yaml_manifest:
        if yaml is not None:
            try:
                # Fazemos o parse só para validar; geração de resources acontece na função auxiliar
                yaml.safe_load(yaml_manifest)
            except Exception as e:
                return jsonify({"ok": False, "result": f"YAML inválido: {e}"}), 400
        # Se yaml==None, seguimos (sem validar) pois o front já tentou avisar.

    # Monta recursos (um por node) — segura mesmo sem YAML (retorna [])
    resources = _resources_from_clab_yaml(yaml_manifest)
    nodes_count = len(resources)

    # Identidade do "owner" (seguro para PoC)
    try:
        owner = getattr(current_user, "username", None) or getattr(current_user, "email", None) or "anonymous"
    except Exception:
        owner = "anonymous"

    # ID e timestamps
    new_id = f"clab-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    created_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    # Atualiza mocks (lista e detalhe)
    CLABS.append({
        "lab_instance_id": new_id,
        "user": owner,
        "title": name or "Unnamed",
        "name": name or "Unnamed",
        "created": created_str,
        "status": "Running",
        "nodes": nodes_count,
    })

    CLABS_DETAILS[new_id] = {
        "lab_instance_id": new_id,
        "title": name or "Unnamed",
        "user": owner,
        "created": created_str,
        "expires_at": None,
        "resources": resources,
        "files": files_meta,
    }

    # Persistência (PoC)
    try:
        save_state(default_state_path(current_app.config.get("CLABS_STATE_PATH")), CLABS, CLABS_DETAILS)
    except Exception:
        current_app.logger.warning("Failed to save CLabs state", exc_info=True)

    return jsonify({
        "ok": True,
        "result": f"CLab '{name or 'Unnamed'}' created (mock).",
        "received": {
            "id": new_id,
            "namespace": namespace or "--",
            "has_yaml": bool(yaml_manifest),
            "files": files_count,
            "resources": nodes_count,
        }
    }), 201

@blueprint.route("/api/list", methods=["GET"])
def api_list():
    expire_days = current_app.config.get("CLABS_EXPIRE_DAYS", 0)
    removed = 0
    if expire_days and expire_days > 0:
        removed = prune_inplace(CLABS, CLABS_DETAILS, max_days=expire_days)
        if removed:
            try:
                save_state(default_state_path(current_app.config.get("CLABS_STATE_PATH")), CLABS, CLABS_DETAILS)
            except Exception:
                current_app.logger.warning("Failed to save after prune", exc_info=True)

    items = []
    for c in CLABS:
        row = _norm_lab_row(c)
        if row["lab_instance_id"]:
            items.append(row)
    return jsonify({"items": items, "pruned": removed})

@blueprint.route("/api/delete/<clab_id>", methods=["DELETE"])
def api_delete(clab_id):
    """Remove (mock) o CLab da memória."""
    removed = False

    # remove de CLABS
    for i in range(len(CLABS) - 1, -1, -1):
        _id = CLABS[i].get("id") or CLABS[i].get("lab_instance_id") or CLABS[i].get("lab_id")
        if _id == clab_id:
            CLABS.pop(i)
            removed = True

    # remove detalhe
    if clab_id in CLABS_DETAILS:
        CLABS_DETAILS.pop(clab_id, None)
        removed = True

    if not removed:
        return jsonify({"ok": False, "result": "not found"}), 404

    # Persistência após remoção — alinhado corretamente
    try:
        save_state(default_state_path(current_app.config.get("CLABS_STATE_PATH")), CLABS, CLABS_DETAILS)
    except Exception:
        current_app.logger.warning("Failed to save CLabs state after delete", exc_info=True)

    return jsonify({"ok": True})

