import os
import json
from typing import Tuple, Dict, List, Optional

def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

def default_state_path(cfg_path: Optional[str] = None) -> str:
    if cfg_path:
        return cfg_path
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))  # .../apps
    return os.path.join(base, "data", "clabs_state.json")

def load_state(state_path: str) -> Tuple[List[dict], Dict[str, dict]]:
    """
    Carrega estado do disco. Se não existir ou falhar, retorna ([], {}).
    """
    try:
        if not os.path.exists(state_path):
            return [], {}
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        clabs = data.get("clabs", [])
        details = data.get("details", {})
        if not isinstance(clabs, list): clabs = []
        if not isinstance(details, dict): details = {}
        return clabs, details
    except Exception:
        return [], {}

def save_state(state_path: str, clabs: List[dict], details: Dict[str, dict]) -> None:
    """
    Salva estado de forma atômica (JSON). Cria a pasta se não existir.
    """
    _ensure_dir(state_path)
    tmp = state_path + ".tmp"
    payload = {"clabs": clabs, "details": details}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path)

def prune_inplace(clabs: List[dict], details: Dict[str, dict], max_days: int = 0) -> int:
    """
    Remoção simplificada (PoC). max_days=0 desativa. Aqui só remove status 'expired'/'deleted'.
    """
    if not max_days or max_days <= 0:
        return 0
    ids = []
    for c in clabs:
        status = (c.get("status") or "").lower()
        if status in {"expired", "deleted"}:
            _id = c.get("id") or c.get("lab_instance_id") or c.get("lab_id")
            if _id:
                ids.append(_id)
    if not ids: return 0
    idset = set(ids)
    clabs[:] = [c for c in clabs if (c.get("id") or c.get("lab_instance_id") or c.get("lab_id")) not in idset]
    for k in list(details.keys()):
        if k in idset:
            details.pop(k, None)
    return len(ids)
