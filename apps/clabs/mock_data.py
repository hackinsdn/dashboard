from typing import List, Dict
from .store import load_state, default_state_path

# mocks padrão (usados se não houver estado persistido)
CLABS: List[dict] = [
    {
        "id": "clab-sample-1",
        "name": "Sample Topology",
        "owner": "demo@example.com",
        "status": "Running",
        "created_at": "2025-09-20 12:00:00",
        "nodes": 2,
    }
]

CLABS_DETAILS: Dict[str, dict] = {
    "clab-sample-1": {
        "lab_instance_id": "clab-sample-1",
        "title": "Sample Topology",
        "user": "demo@example.com",
        "created": "2025-09-20 12:00:00",
        "expires_at": None,
        "resources": [
            {"kind": "node", "name": "r1", "ready": "Running", "node_name": "linux", "pod_ip": "172.20.20.2", "age": "--", "services": [], "links": []},
            {"kind": "node", "name": "r2", "ready": "Running", "node_name": "linux", "pod_ip": "172.20.20.3", "age": "--", "services": [], "links": []},
        ],
        "files": []
    }
}

# tenta carregar estado persistido
try:
    path = default_state_path()  # --> .../apps/data/clabs_state.json
    _clabs, _details = load_state(path)
    if _clabs or _details:
        CLABS[:] = _clabs
        CLABS_DETAILS.clear()
        CLABS_DETAILS.update(_details)
except Exception:
    # mantém mocks se falhar
    pass
