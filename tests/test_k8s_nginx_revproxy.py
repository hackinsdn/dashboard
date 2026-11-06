"""Pytest suite for scripts/k8s_nginx_revproxy/k8s_nginx_revproxy.py.

Focuses on the NetworkPolicy feature: CIDR normalisation, the policy body
rendered from a Service's selector/uid, the config-hash that gates rewrites,
and the reconcile loop (create / no-op / update / delete, plus 404 and 409
handling) driven against a fake NetworkingV1Api.

The daemon is a standalone script, not part of the Flask app, so this module
loads it by file path and stubs the ``kubernetes`` client when it is not
installed -- the logic under test never talks to a real cluster.

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_k8s_nginx_revproxy.py -v
"""

import importlib.util
import io
import os
import sys
import types
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "k8s_nginx_revproxy", "k8s_nginx_revproxy.py")
HASH_ANN = "revproxy.hackinsdn.io/config-hash"


def _ensure_kubernetes_stub():
    """Make `import kubernetes` succeed even where the real client is absent.
    The tests construct the Reconciler by hand and inject a fake networking API,
    so only the module-level `from kubernetes import ...` needs to resolve."""
    try:
        import kubernetes  # noqa: F401
        return
    except Exception:
        pass
    k = types.ModuleType("kubernetes")
    cl = types.ModuleType("kubernetes.client")
    cfg = types.ModuleType("kubernetes.config")
    rest = types.ModuleType("kubernetes.client.rest")

    class ApiException(Exception):
        def __init__(self, status=0, reason=""):
            super().__init__(f"{status} {reason}")
            self.status = status
            self.reason = reason

    rest.ApiException = ApiException
    cl.rest = rest
    cl.Configuration = lambda *a, **k: SimpleNamespace()
    cl.ApiClient = lambda *a, **k: SimpleNamespace()
    cl.CoreV1Api = lambda *a, **k: SimpleNamespace()
    cl.NetworkingV1Api = lambda *a, **k: SimpleNamespace()
    cfg.load_kube_config = lambda *a, **k: None
    k.client = cl
    k.config = cfg
    sys.modules.update({
        "kubernetes": k, "kubernetes.client": cl,
        "kubernetes.client.rest": rest, "kubernetes.config": cfg,
    })


def _load_module():
    _ensure_kubernetes_stub()
    spec = importlib.util.spec_from_file_location("k8s_nginx_revproxy", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["k8s_nginx_revproxy"] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load_module()
CIDRS = ["10.0.0.1/32", "192.168.0.0/24"]


# --- fakes ------------------------------------------------------------------
def _svc(name, ns, uid, selector, ports, type="NodePort", annotations=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=ns, uid=uid,
                                 annotations=annotations or {}),
        spec=SimpleNamespace(type=type, selector=selector, ports=ports),
    )


def _port(p, node_port, name=None, protocol="TCP"):
    return SimpleNamespace(port=p, node_port=node_port, name=name, protocol=protocol)


def _pod(labels, host_ip):
    return SimpleNamespace(
        metadata=SimpleNamespace(labels=labels),
        status=SimpleNamespace(host_ip=host_ip, phase="Running"),
    )


def _settings(**kw):
    base = dict(kubeconfigs=["kc"], namespaces=["ns"], proxy_domain="d.example")
    base.update(kw)
    return m.Settings(**base)


def _recon(settings):
    r = m.Reconciler.__new__(m.Reconciler)
    r.s = settings
    r._np_disabled = False
    r._np_cleanup_done = False
    return r


def _existing_np(name, ns, hash_val, rv="7"):
    return SimpleNamespace(metadata=SimpleNamespace(
        name=name, namespace=ns, annotations={HASH_ANN: hash_val}, resource_version=rv))


class FakeNet:
    def __init__(self, existing=None, raise_on=None):
        self.existing = existing or {}        # ns -> [np objects]
        self.raise_on = raise_on or {}        # verb -> exception
        self.calls = []

    def list_namespaced_network_policy(self, namespace, label_selector, _request_timeout=None):
        self.calls.append(("list", namespace, label_selector))
        if "list" in self.raise_on:
            raise self.raise_on["list"]
        return SimpleNamespace(items=list(self.existing.get(namespace, [])))

    def create_namespaced_network_policy(self, namespace, body, _request_timeout=None):
        self.calls.append(("create", namespace, body["metadata"]["name"]))
        if "create" in self.raise_on:
            raise self.raise_on["create"]

    def replace_namespaced_network_policy(self, name, namespace, body, _request_timeout=None):
        self.calls.append(("replace", namespace, name, body["metadata"].get("resourceVersion")))
        if "replace" in self.raise_on:
            raise self.raise_on["replace"]

    def delete_namespaced_network_policy(self, name, namespace, _request_timeout=None):
        self.calls.append(("delete", namespace, name))
        if "delete" in self.raise_on:
            raise self.raise_on["delete"]


class FakePool:
    def __init__(self, net):
        self._net = net
        self.current_path = "kc"
        self.rotated = 0

    def networking(self):
        return self._net

    def rotate(self):
        self.rotated += 1


# --- CIDR normalisation -----------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("10.0.0.1", "10.0.0.1/32"),
    ("2001:db8::1", "2001:db8::1/128"),
    ("10.0.0.0/24", "10.0.0.0/24"),
    ("  10.0.0.1 ", "10.0.0.1/32"),
])
def test_normalize_cidr(raw, expected):
    assert m._normalize_cidr(raw) == expected


def test_normalize_cidr_rejects_garbage():
    with pytest.raises(ValueError):
        m._normalize_cidr("not-an-ip")


# --- NetworkPolicy body -----------------------------------------------------
def test_to_body_matches_template():
    spec = m.NetworkPolicySpec("ns", "webui", "uid-123", {"app": "webui"})
    body = spec.to_body(CIDRS, HASH_ANN)

    assert body["apiVersion"] == "networking.k8s.io/v1"
    assert body["kind"] == "NetworkPolicy"
    assert body["metadata"]["name"] == "webui"
    assert body["metadata"]["namespace"] == "ns"
    assert body["metadata"]["labels"][m.MANAGED_BY_KEY] == m.MANAGED_BY_VAL
    assert HASH_ANN in body["metadata"]["annotations"]

    owner = body["metadata"]["ownerReferences"][0]
    assert owner == {"apiVersion": "v1", "kind": "Service", "name": "webui", "uid": "uid-123"}

    assert body["spec"]["podSelector"] == {"matchLabels": {"app": "webui"}}
    assert body["spec"]["policyTypes"] == ["Ingress"]
    assert body["spec"]["ingress"] == [
        {"from": [{"ipBlock": {"cidr": "10.0.0.1/32"}},
                  {"ipBlock": {"cidr": "192.168.0.0/24"}}]}
    ]


def test_config_hash_sensitivity():
    spec = m.NetworkPolicySpec("ns", "webui", "uid-123", {"app": "webui"})
    base = spec.config_hash(CIDRS)
    assert base == spec.config_hash(list(CIDRS))                         # stable
    assert base != spec.config_hash(["10.0.0.2/32"])                    # cidrs matter
    other = m.NetworkPolicySpec("ns", "webui", "uid-123", {"app": "x"})
    assert base != other.config_hash(CIDRS)                             # selector matters


# --- service -> (vhosts, npspec) --------------------------------------------
def test_service_to_vhosts_builds_npspec_when_enabled():
    r = _recon(_settings(allow_cidrs=["10.0.0.1/32"]))
    pods = {"ns": [_pod({"app": "webui"}, "10.0.0.5")]}
    svc = _svc("webui", "ns", "uid-9", {"app": "webui"}, [_port(8080, 31000)])
    vhosts, np = r._service_to_vhosts(svc, pods)
    assert [v.hostname for v in vhosts] == ["8080-webui.d.example"]
    assert np is not None
    assert np.namespace == "ns" and np.name == "webui" and np.uid == "uid-9"
    assert np.selector == {"app": "webui"}


def test_service_to_vhosts_no_policy_when_disabled():
    r = _recon(_settings(allow_cidrs=[]))
    pods = {"ns": [_pod({"app": "webui"}, "10.0.0.5")]}
    svc = _svc("webui", "ns", "uid-9", {"app": "webui"}, [_port(8080, 31000)])
    vhosts, np = r._service_to_vhosts(svc, pods)
    assert vhosts and np is None


def test_service_to_vhosts_skipped_service_has_no_policy():
    r = _recon(_settings(allow_cidrs=["10.0.0.1/32"]))
    pods = {"ns": [_pod({"app": "webui"}, "10.0.0.5")]}
    svc = _svc("other", "ns", "uid-1", {"app": "nomatch"}, [_port(80, 31001)])
    vhosts, np = r._service_to_vhosts(svc, pods)
    assert vhosts == [] and np is None


# --- reconcile_network_policies ---------------------------------------------
def _spec():
    return m.NetworkPolicySpec("ns", "webui", "uid-123", {"app": "webui"})


def test_reconcile_creates_missing_policy():
    net = FakeNet()
    r = _recon(_settings(allow_cidrs=CIDRS)); r.pool = FakePool(net)
    r.reconcile_network_policies({("ns", "webui"): _spec()})
    assert ("create", "ns", "webui") in net.calls


def test_reconcile_noop_when_hash_matches():
    spec = _spec()
    net = FakeNet(existing={"ns": [_existing_np("webui", "ns", spec.config_hash(CIDRS))]})
    r = _recon(_settings(allow_cidrs=CIDRS)); r.pool = FakePool(net)
    r.reconcile_network_policies({("ns", "webui"): spec})
    assert not any(c[0] in ("create", "replace", "delete") for c in net.calls)


def test_reconcile_replaces_on_hash_change():
    net = FakeNet(existing={"ns": [_existing_np("webui", "ns", "stale", rv="42")]})
    r = _recon(_settings(allow_cidrs=CIDRS)); r.pool = FakePool(net)
    r.reconcile_network_policies({("ns", "webui"): _spec()})
    assert ("replace", "ns", "webui", "42") in net.calls


def test_reconcile_deletes_stale_policy():
    net = FakeNet(existing={"ns": [_existing_np("old", "ns", "h")]})
    r = _recon(_settings(allow_cidrs=CIDRS)); r.pool = FakePool(net)
    r.reconcile_network_policies({})  # nothing desired
    assert ("delete", "ns", "old") in net.calls


def test_reconcile_delete_404_is_tolerated():
    net = FakeNet(existing={"ns": [_existing_np("old", "ns", "h")]},
                  raise_on={"delete": m.ApiException(status=404)})
    r = _recon(_settings(allow_cidrs=CIDRS)); r.pool = FakePool(net)
    r.reconcile_network_policies({})
    assert r.pool.rotated == 0  # 404 swallowed, not treated as connectivity error


def test_reconcile_create_409_is_tolerated():
    net = FakeNet(raise_on={"create": m.ApiException(status=409)})
    r = _recon(_settings(allow_cidrs=CIDRS)); r.pool = FakePool(net)
    r.reconcile_network_policies({("ns", "webui"): _spec()})
    assert r.pool.rotated == 0  # unmanaged same-named policy: warn, do not clobber


def test_reconcile_skips_and_disables_on_403():
    net = FakeNet(raise_on={"list": m.ApiException(status=403)})
    r = _recon(_settings(allow_cidrs=CIDRS)); r.pool = FakePool(net)
    assert r.reconcile_network_policies({("ns", "webui"): _spec()}) is False
    assert r._np_disabled is True            # management turned off for the run
    assert r.pool.rotated == 0               # 403 is not a connectivity error
    assert not any(c[0] == "create" for c in net.calls)


# --- reconcile_once gating (feature on/off) ---------------------------------
def _drive_once(r, netpols):
    """Run one reconcile cycle with fetch/apply stubbed so only the
    NetworkPolicy gating is exercised."""
    r.fetch_desired = lambda: m.Desired(vhosts=[], netpols=netpols)
    r.render = lambda vhosts: {"00-maps.conf": "x\n"}
    r.apply = lambda files: True
    r.reconcile_once()


def test_reconcile_once_cleans_up_when_feature_disabled():
    # A managed policy exists on the cluster, but --allow-cidr is now unset.
    net = FakeNet(existing={"ns": [_existing_np("webui", "ns", "h")]})
    r = _recon(_settings(allow_cidrs=[])); r.pool = FakePool(net)

    _drive_once(r, netpols={})
    assert ("delete", "ns", "webui") in net.calls   # stray policy removed
    assert r._np_cleanup_done is True

    # A second cycle must not re-list / re-delete -- cleanup is one-shot.
    net.calls.clear()
    _drive_once(r, netpols={})
    assert net.calls == []


def test_reconcile_once_enabled_creates_policy():
    net = FakeNet()
    r = _recon(_settings(allow_cidrs=CIDRS)); r.pool = FakePool(net)
    _drive_once(r, netpols={("ns", "webui"): _spec()})
    assert ("create", "ns", "webui") in net.calls


# --- dry run ----------------------------------------------------------------
def test_dry_run_includes_network_policy():
    r = _recon(_settings(allow_cidrs=CIDRS))
    buf = io.StringIO()
    with redirect_stdout(buf):
        r.dry_run({"00-maps.conf": "x\n"}, {("ns", "webui"): _spec()})
    out = buf.getvalue()
    assert "NetworkPolicy" in out
    assert "webui" in out
    assert "10.0.0.1/32" in out
