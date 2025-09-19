# -*- encoding: utf-8 -*-

# Lista simples (usada na página de Running CLabs)
CLABS = [
    {
        "id": "clab-001",
        "name": "Edge Fabric",
        "owner": "alice@example.com",
        "status": "running",
        "created_at": "2025-09-10 14:32",
        "nodes": 6,
    },
    {
        "id": "clab-002",
        "name": "Core BGP",
        "owner": "bob@example.com",
        "status": "running",
        "created_at": "2025-09-12 09:05",
        "nodes": 4,
    },
]

# Recursos detalhados por CLab (para a página Open CLab)
# Estrutura inspirada em view_lab_instance: uma lista de "resources" por nó/serviço
CLABS_DETAILS = {
    "clab-001": {
        "title": "Edge Fabric",
        "lab_instance_id": "clab-001",
        "user": "Alice (alice@example.com)",
        "created": "2025-09-10 14:32",
        "expires_at": None,  # PoC: sem expiração
        "resources": [
            {
                "kind": "node",
                "name": "leaf1",
                "ready": "Running",
                "links": [
                    {"label": "Console", "href": "#"},
                ],
                "services": [
                    {"name": "Mgmt SSH", "url": "#"},
                    {"name": "gNMI", "url": "#"},
                ],
                "age": "1d",
                "node_name": "edge-host-01",
                "pod_ip": "10.0.0.11",
            },
            {
                "kind": "node",
                "name": "leaf2",
                "ready": "Running",
                "links": [{"label": "Console", "href": "#"}],
                "services": [{"name": "Mgmt SSH", "url": "#"}],
                "age": "1d",
                "node_name": "edge-host-02",
                "pod_ip": "10.0.0.12",
            },
        ],
    },
    "clab-002": {
        "title": "Core BGP",
        "lab_instance_id": "clab-002",
        "user": "Bob (bob@example.com)",
        "created": "2025-09-12 09:05",
        "expires_at": None,
        "resources": [
            {
                "kind": "node",
                "name": "r1",
                "ready": "Running",
                "links": [{"label": "Console", "href": "#"}],
                "services": [{"name": "Mgmt SSH", "url": "#"}],
                "age": "2h",
                "node_name": "core-host-01",
                "pod_ip": "10.1.0.21",
            },
            {
                "kind": "node",
                "name": "r2",
                "ready": "Running",
                "links": [{"label": "Console", "href": "#"}],
                "services": [{"name": "Mgmt SSH", "url": "#"}],
                "age": "2h",
                "node_name": "core-host-02",
                "pod_ip": "10.1.0.22",
            },
        ],
    },
}
