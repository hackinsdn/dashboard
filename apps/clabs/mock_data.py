from typing import List, Dict
from .store import load_state, default_state_path

# mocks padrão (usados se não houver estado persistido)
CLABS: List[dict] = []

CLABS_DETAILS: Dict[str, dict] = {}

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
