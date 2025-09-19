# -*- encoding: utf-8 -*-
from flask import render_template, request, jsonify
from flask_login import current_user
from datetime import datetime

from . import blueprint
from .mock_data import CLABS, CLABS_DETAILS

# Tentamos usar PyYAML; se não estiver instalado, seguimos sem quebrar
try:
    import yaml  # PyYAML
except Exception:
    yaml = None

@blueprint.route("/running")
def running():
    labs = []
    for c in CLABS:
        labs.append({
            "lab_instance_id": c["id"],        
            "user": c.get("owner", "--"),      
            "title": c.get("name", "--"),      
            "created": c.get("created_at", "--"), 
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
        # cfg costuma ser dict com 'kind', 'image', 'mgmt_ipv4' etc.
        kind = "node"
        ready = "Running"  # PoC: considera subido
        pod_ip = None
        node_name = None
        links = [{"label": "Console", "href": "#"}]  # placeholder
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
    # Recebe dados do formulário e arquivos (mock – não persiste em disco)
    name = (request.form.get("clab_name") or "").strip()
    namespace = (request.form.get("clab_namespace") or "").strip()
    yaml_manifest = (request.form.get("clab_yaml") or "").strip()

    # Conta arquivos enviados (arquivos soltos + pastas via webkitdirectory)
    files_count = 0
    files_list = []  # Vamos armazenar os nomes dos arquivos enviados aqui
    try:
        # Adiciona arquivos individuais
        files_list.extend(request.files.getlist("clab_files") or [])

        # Adiciona arquivos de pastas com caminho completo
        for file in request.files.getlist("clab_folders"):
            # O caminho completo da pasta será adicionado, evitando duplicação
            files_list.append(file)

        files_count = len(files_list)
    except Exception:
        files_count = 0

    # Validação mínima (PoC): exigir YAML ou ao menos 1 arquivo
    if not yaml_manifest and files_count == 0:
        return jsonify({"ok": False, "result": "Provide YAML and/or files"}), 400

    # ---- Monta recursos a partir do YAML (se possível) ----
    resources = _resources_from_clab_yaml(yaml_manifest)
    nodes_count = len(resources)

    # ---- Atualiza os mocks para aparecer em Running CLabs e Open CLab ----
    new_id = f"clab-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    created_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    # Dados do "owner" a partir do usuário logado (fallbacks seguros)
    owner_name = getattr(current_user, "name", None) or getattr(current_user, "username", "User")
    owner_email = getattr(current_user, "email", None) or "user@example.com"
    owner_display = f"{owner_name} ({owner_email})"

    # Lista simples (Running CLabs)
    CLABS.append({
        "id": new_id,
        "name": name or "Unnamed",
        "owner": owner_email,
        "status": "running",
        "created_at": created_str,
        "nodes": nodes_count,
    })

    # Detalhes (Open CLab) — Incluindo os arquivos enviados
    CLABS_DETAILS[new_id] = {
        "title": name or "Unnamed",
        "lab_instance_id": new_id,
        "user": owner_display,
        "created": created_str,
        "expires_at": None,
        "resources": resources,  # agora populado a partir do YAML
        "files": files_list,  # Armazena os arquivos enviados com o caminho completo
    }

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
