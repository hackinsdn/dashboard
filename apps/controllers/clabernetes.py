# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import datetime as dt
import threading
from typing import List, Dict, Optional, Any

import yaml  # requer PyYAML no requirements.txt


class C9sController:
    """
    Controller para gerenciar ContainerLabs (PoC):
    - Estado em memória com persistência opcional em arquivo JSON
    - Operações CRUD mínimas + TTL prune
    - Parse de YAML ContainerLab -> resources (nós)
    """

    def __init__(
        self,
        state_path: Optional[str] = None,
        expire_days: int = 0,
        upload_max_files: int = 200,
    ) -> None:
        self._lock = threading.RLock()
        self.state_path = state_path or None
        self.expire_days = int(expire_days or 0)
        self.upload_max_files = int(upload_max_files or 200)

        self._list: List[Dict[str, Any]] = []
        self._details: Dict[str, Dict[str, Any]] = {}

        self._load_state()

    # -------------------- Persistência PoC --------------------

    def _state_file(self) -> Optional[str]:
        if not self.state_path:
            return None
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        return self.state_path

    def _load_state(self) -> None:
        path = self._state_file()
        if not path or not os.path.exists(path):
            self._list = []
            self._details = {}
            return
        try:
            with self._lock, open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            self._list = payload.get("list", []) or []
            self._details = payload.get("details", {}) or {}
        except Exception:
            # Em caso de erro de leitura, inicia vazio (não quebra a app)
            self._list = []
            self._details = {}

    def _save_state(self) -> None:
        path = self._state_file()
        if not path:
            return
        payload = {"list": self._list, "details": self._details}
        tmp = path + ".tmp"
        with self._lock, open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # -------------------- Helpers --------------------

    @staticmethod
    def _normalize_row(c: dict) -> dict:
        lab_id = c.get("id") or c.get("lab_instance_id") or c.get("lab_id")
        user = c.get("owner") or c.get("user") or "--"
        title = c.get("name") or c.get("title") or "--"
        created = c.get("created_at") or c.get("created") or "--"
        return {
            "lab_instance_id": lab_id,
            "user": user,
            "title": title,
            "created": created,
        }

    @staticmethod
    def _parse_created(val: Optional[str]) -> Optional[dt.datetime]:
        if not val:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return dt.datetime.strptime(val, fmt)
            except Exception:
                continue
        try:
            return dt.datetime.fromisoformat(val.replace("Z", ""))
        except Exception:
            return None

    # -------------------- API pública --------------------

    def list(self, user: Optional[str] = None) -> List[dict]:
        """
        Retorna linhas normalizadas para tabela. Se `user` for fornecido,
        o filtro por dono pode ser feito aqui (ou nas rotas/RBAC externo).
        """
        items = []
        for c in self._list:
            # se quiser filtrar por owner, descomente:
            # if user and (c.get("owner") or c.get("user")) != user:
            #     continue
            row = self._normalize_row(c)
            if row["lab_instance_id"]:
                items.append(row)
        return items

    def get(self, lab_id: str) -> Optional[dict]:
        return self._details.get(lab_id)

    def delete(self, lab_id: str) -> bool:
        with self._lock:
            if lab_id not in self._details:
                return False
            # remove detalhes
            self._details.pop(lab_id, None)
            # remove da lista
            self._list = [
                c
                for c in self._list
                if (c.get("id") or c.get("lab_instance_id") or c.get("lab_id")) != lab_id
            ]
            self._save_state()
            return True

    def clear(self) -> int:
        with self._lock:
            n = len(self._details)
            self._list.clear()
            self._details.clear()
            self._save_state()
            return n

    def prune(self, now: Optional[dt.datetime] = None) -> int:
        """Remove labs criados há mais de `expire_days` (se > 0)."""
        days = self.expire_days
        if not days or days <= 0:
            return 0

        cutoff = (now or dt.datetime.utcnow()) - dt.timedelta(days=days)

        ids_to_remove: List[str] = []
        for c in self._list:
            created_val = c.get("created_at") or c.get("created")
            ts = self._parse_created(created_val)
            if ts and ts < cutoff:
                _id = c.get("id") or c.get("lab_instance_id") or c.get("lab_id")
                if _id:
                    ids_to_remove.append(_id)

        if not ids_to_remove:
            return 0

        ids = set(ids_to_remove)
        with self._lock:
            self._list = [
                c
                for c in self._list
                if (c.get("id") or c.get("lab_instance_id") or c.get("lab_id")) not in ids
            ]
            for cid in list(self._details.keys()):
                if cid in ids:
                    self._details.pop(cid, None)
            self._save_state()

        return len(ids)

    # -------------------- Criação & YAML --------------------

    def _resources_from_yaml(self, yaml_text: str) -> List[dict]:
        """
        Constrói a lista de resources (nós) a partir de um YAML ContainerLab.
        """
        if not yaml_text:
            return []

        data = yaml.safe_load(yaml_text)
        if not isinstance(data, dict):
            return []

        topo = data.get("topology") or {}
        nodes = topo.get("nodes") or {}
        if not isinstance(nodes, dict):
            return []

        resources = []
        for name, cfg in nodes.items():
            pod_ip = None
            node_name = None
            if isinstance(cfg, dict):
                pod_ip = cfg.get("mgmt_ipv4")
                node_name = cfg.get("kind") or cfg.get("image") or "--"

            resources.append(
                {
                    "kind": "node",
                    "name": str(name),
                    "ready": "Running",
                    "links": [{"label": "Console", "href": "#"}],
                    "services": [],
                    "age": "--",
                    "node_name": node_name or "--",
                    "pod_ip": pod_ip or "--",
                }
            )

        return resources

    def create(
        self,
        name: str,
        namespace: str,
        yaml_manifest: str,
        files_meta: Optional[List[dict]],
        owner: Optional[str],
    ) -> dict:
        """
        Cria um novo CLab (PoC), valida YAML e atualiza estado.
        Retorna o detalhe criado.
        """
        if yaml_manifest:
            # valida sintaxe (raise se inválido)
            yaml.safe_load(yaml_manifest)

        resources = self._resources_from_yaml(yaml_manifest)
        nodes_count = len(resources)

        now = dt.datetime.utcnow()
        new_id = f"clab-{now.strftime('%Y%m%d%H%M%S')}"
        created_str = now.strftime("%Y-%m-%d %H:%M:%S")
        _owner = owner or "anonymous"
        _name = name or "Unnamed"

        header = {
            "id": new_id,
            "lab_instance_id": new_id,
            "owner": _owner,
            "user": _owner,
            "title": _name,
            "name": _name,
            "created_at": created_str,
            "created": created_str,
            "status": "Running",
            "nodes": nodes_count,
        }

        detail = {
            "lab_instance_id": new_id,
            "title": _name,
            "user": _owner,
            "created": created_str,
            "expires_at": None,
            "resources": resources,
            "files": files_meta or [],
            "namespace": namespace or "--",
        }

        with self._lock:
            self._list.append(header)
            self._details[new_id] = detail
            self._save_state()

        return detail
