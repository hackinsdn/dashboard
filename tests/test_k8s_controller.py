"""Pytest suite for apps/controllers/kubernetes.py (K8sController).

Targets the pure-logic and thinly-wrapped methods (the heavy create_lab /
get_lab_resources / get_statistics orchestration is out of scope). A fixture
builds a fully-initialised controller without a real cluster: it sets a dummy
K8S_NAMESPACE and mocks kube config loading + the client API classes, so the
constructor wires self.identifiers / self.nodes / self.v1_api (a MagicMock).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_k8s_controller.py -v
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import types
from unittest.mock import MagicMock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# --- isolate the test run from any real data --------------------------------
TEST_DATA_DIR = tempfile.mkdtemp(prefix="hackinsdn_test_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ.setdefault("OPTIONAL_MODULES", "")

# stub the clabernetes controller (needs a 'clabverter' binary at import time)
_fake_clabernetes = types.ModuleType("apps.controllers.clabernetes")


class _StubC9sController:
    def __getattr__(self, name):
        raise NotImplementedError("clabernetes stub - not needed for these tests")


_fake_clabernetes.C9sController = _StubC9sController
sys.modules["apps.controllers.clabernetes"] = _fake_clabernetes

from run import app as flask_app  # noqa: E402

# real submodule (the package name `kubernetes`/`k8s` proxy would shadow it)
k8s_module = importlib.import_module("apps.controllers.kubernetes")
K8sController = k8s_module.K8sController

flask_app.config["TESTING"] = True


# --- fixtures ----------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _cleanup_temp_data_dir():
    yield
    import shutil

    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _app_context():
    with flask_app.app_context():
        yield


@pytest.fixture()
def ctrl(monkeypatch):
    """A fully-wired K8sController with the kube client mocked out."""
    monkeypatch.setattr(k8s_module.app_config, "K8S_NAMESPACE", "test-ns")
    monkeypatch.setattr(k8s_module.config, "load_kube_config", lambda **k: None)
    monkeypatch.setattr(k8s_module.client, "CoreV1Api", lambda: MagicMock())
    monkeypatch.setattr(k8s_module.client, "AppsV1Api", lambda: MagicMock())
    monkeypatch.setattr(k8s_module.client, "ApiClient", lambda: MagicMock())
    controller = K8sController()
    return controller


def _fake_node(name, ready=True, ip="10.0.0.1"):
    cond = types.SimpleNamespace(type="Ready", status="True" if ready else "False")
    addr = types.SimpleNamespace(type="InternalIP", address=ip)
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name),
        status=types.SimpleNamespace(conditions=[cond], addresses=[addr]),
    )


# --- pure logic ---------------------------------------------------------
class TestPureLogic:
    def test_try_get_app(self, ctrl):
        assert ctrl.try_get_app("") == "http://"
        assert ctrl.try_get_app("https-web") == "https://"
        assert ctrl.try_get_app("ssh-console") == "ssh://"
        assert ctrl.try_get_app("mystery") == "http://"

    def test_humanbytes(self, ctrl):
        assert "KB" in ctrl.humanbytes(2048)
        assert "MB" in ctrl.humanbytes(5 * 1024 * 1024)
        assert "GB" in ctrl.humanbytes(3 * 1024**3)

    def test_validate_token_and_pods_by_lab(self, ctrl):
        assert ctrl.validate_token("anything") is True
        assert ctrl.get_pods_by_lab_id("lab1") == []

    def test_get_pod_hash(self, ctrl):
        assert ctrl.get_pod_hash(pod_hash="fixed") == "fixed"
        generated = ctrl.get_pod_hash()
        assert len(generated) == 14

    def test_allowed_nodes_branches(self, ctrl):
        assert ctrl.get_allowed_nodes(dry_run=True) == ["nodeA", "nodeB", "nodeC"]
        assert ctrl.get_allowed_nodes(allowed_nodes=["x", "y"]) == ["x", "y"]
        assert ctrl.get_allowed_nodes_str(allowed_nodes=["x", "y"]) == "x,y"

    def test_choose_one_node_branches(self, ctrl):
        assert ctrl.choose_one_node(dry_run=True) == "nodeA"
        assert ctrl.choose_one_node(allowed_nodes=["only"]) == "only"

    def test_get_identifier_func(self, ctrl):
        assert ctrl.get_identifier_func("pod_hash") == ctrl.get_pod_hash
        assert ctrl.get_identifier_func("bogus") is None

    def test_substitute_identifiers_ok(self, ctrl):
        status, data = ctrl.substitute_identifiers(
            "id=${pod_hash}", pod_hash="abc", dry_run=True
        )
        assert status is True
        assert data == "id=abc"

    def test_substitute_identifiers_invalid_placeholder(self, ctrl):
        status, data = ctrl.substitute_identifiers("x=${bogus}", dry_run=True)
        assert status is False
        assert "Invalid placeholder bogus" in data


# --- registry secret ----------------------------------------------------
class TestRegistrySecret:
    def test_success(self, ctrl):
        ctrl.v1_api.create_namespaced_secret = MagicMock()
        status, msg = ctrl.create_registry_secret("s1", "reg.io", "bob", "pw")
        assert status is True
        ctrl.v1_api.create_namespaced_secret.assert_called_once()

    def test_failure(self, ctrl):
        ctrl.v1_api.create_namespaced_secret = MagicMock(
            side_effect=RuntimeError("boom")
        )
        status, msg = ctrl.create_registry_secret("s1", "reg.io", "bob", "pw")
        assert status is False
        assert "Failed to create secret" in msg


# --- get_*_by_name ------------------------------------------------------
class TestGetByName:
    def test_get_pod_running(self, ctrl):
        pod = MagicMock()
        pod.to_dict.return_value = {"metadata": {"name": "p1"}}
        pod.status.phase = "Running"
        ctrl.v1_api.read_namespaced_pod = MagicMock(return_value=pod)
        result = ctrl.get_pod_by_name({"name": "p1"})
        assert result["is_ok"] is True

    def test_get_pod_not_running(self, ctrl):
        pod = MagicMock()
        pod.to_dict.return_value = {}
        pod.status.phase = "Pending"
        ctrl.v1_api.read_namespaced_pod = MagicMock(return_value=pod)
        result = ctrl.get_pod_by_name({"name": "p1"})
        assert result["is_ok"] is False

    def test_get_service_is_ok(self, ctrl):
        svc = MagicMock()
        svc.to_dict.return_value = {}
        ctrl.v1_api.read_namespaced_service = MagicMock(return_value=svc)
        assert ctrl.get_service_by_name({"name": "s1"})["is_ok"] is True

    def test_get_resources_by_name_dispatch(self, ctrl):
        ctrl.get_pod_by_name = MagicMock(return_value={"kind": "Pod"})
        ctrl.get_service_by_name = MagicMock(return_value={"kind": "Service"})
        results = ctrl.get_resources_by_name(
            [{"kind": "Pod", "name": "p"}, {"kind": "Service", "name": "s"}]
        )
        assert len(results) == 2
        ctrl.get_pod_by_name.assert_called_once()
        ctrl.get_service_by_name.assert_called_once()


# --- delete_*_by_name ---------------------------------------------------
class TestDeleteByName:
    def test_delete_pod_success(self, ctrl):
        ctrl.v1_api.delete_namespaced_pod = MagicMock()
        assert ctrl.delete_pod_by_name({"name": "p1"}) is True

    def test_delete_pod_failure(self, ctrl):
        ctrl.v1_api.delete_namespaced_pod = MagicMock(side_effect=RuntimeError("boom"))
        assert ctrl.delete_pod_by_name({"name": "p1"}) is False

    def test_delete_secret_accepts_dict_and_str(self, ctrl):
        ctrl.v1_api.delete_namespaced_secret = MagicMock()
        assert ctrl.delete_secret_by_name({"name": "sec"}) is True
        assert ctrl.delete_secret_by_name("sec2") is True

    def test_delete_resources_by_name_dispatch(self, ctrl):
        ctrl.delete_pod_by_name = MagicMock(return_value=True)
        ctrl.delete_service_by_name = MagicMock(return_value=True)
        ctrl.delete_secret_by_name = MagicMock(return_value=True)
        results = ctrl.delete_resources_by_name(
            [
                {"kind": "Pod", "name": "p"},
                {"kind": "Service", "name": "s"},
                {"kind": "Secret", "name": "sec"},
            ]
        )
        assert results == [True, True, True]


# --- nodes --------------------------------------------------------------
class TestNodes:
    def test_update_and_get_nodes(self, ctrl):
        ctrl.k8s_avoid_nodes = {"nodeB"}
        resp = types.SimpleNamespace(items=[_fake_node("nodeA"), _fake_node("nodeB")])
        ctrl.v1_api.list_node = MagicMock(return_value=resp)
        ctrl.nodes_last_updated = 0

        nodes = ctrl.get_nodes()
        names = {n["name"]: n["status"] for n in nodes}
        assert names["nodeA"] == "Ready"
        assert names["nodeB"] == "NotReady"  # avoided -> not in ready_nodes

    def test_get_node_ip(self, ctrl):
        resp = types.SimpleNamespace(items=[_fake_node("nodeA", ip="10.1.2.3")])
        ctrl.v1_api.list_node = MagicMock(return_value=resp)
        ctrl.nodes_last_updated = 0

        assert ctrl.get_node_ip("nodeA") == "10.1.2.3"
        assert ctrl.get_node_ip("missing") is None


def _completed(stdout):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)


# --- kubectl / subprocess wrappers --------------------------------------
class TestKubectlResources:
    def test_get_k8s_resource_topology(self, ctrl, monkeypatch):
        """Topology resources take is_ok from status.topologyReady."""
        out = json.dumps({"kind": "Topology", "status": {"topologyReady": True}})
        monkeypatch.setattr(
            k8s_module.subprocess, "run", lambda *a, **k: _completed(out)
        )
        result = ctrl.get_k8s_resource({"kind": "Topology", "name": "t1"})
        assert result["is_ok"] is True

    def test_get_k8s_resource_generic_is_ok(self, ctrl, monkeypatch):
        """Non-Topology resources are always is_ok=True when kubectl succeeds."""
        monkeypatch.setattr(
            k8s_module.subprocess, "run", lambda *a, **k: _completed(json.dumps({}))
        )
        result = ctrl.get_k8s_resource({"kind": "Foo", "name": "f1"})
        assert result["is_ok"] is True

    def test_get_k8s_resource_error(self, ctrl, monkeypatch):
        """A kubectl failure is surfaced as an Exception."""

        def boom(*a, **k):
            raise subprocess.CalledProcessError(1, "kubectl", stderr="nope")

        monkeypatch.setattr(k8s_module.subprocess, "run", boom)
        with pytest.raises(Exception):
            ctrl.get_k8s_resource({"kind": "Foo", "name": "f1"})

    def test_create_k8s_resource_kubectl_fallback(self, ctrl, monkeypatch):
        """Unknown kinds are created via kubectl and the parsed JSON returned."""
        created = {"kind": "Topology", "metadata": {"name": "t1", "uid": "u1"}}
        monkeypatch.setattr(
            k8s_module.subprocess,
            "run",
            lambda *a, **k: _completed(json.dumps(created)),
        )
        result = ctrl.create_k8s_resource({"kind": "Topology", "metadata": {}})
        assert result == created

    def test_create_k8s_resource_error(self, ctrl, monkeypatch):
        def boom(*a, **k):
            raise subprocess.CalledProcessError(1, "kubectl", stderr="bad")

        monkeypatch.setattr(k8s_module.subprocess, "run", boom)
        with pytest.raises(Exception):
            ctrl.create_k8s_resource({"kind": "Topology", "metadata": {}})

    def test_delete_k8s_resource_success(self, ctrl, monkeypatch):
        monkeypatch.setattr(
            k8s_module.subprocess, "run", lambda *a, **k: _completed("")
        )
        assert ctrl.delete_k8s_resource({"kind": "Topology", "name": "t1"}) is True

    def test_delete_k8s_resource_failure(self, ctrl, monkeypatch):
        def boom(*a, **k):
            raise subprocess.CalledProcessError(1, "kubectl", stderr="bad")

        monkeypatch.setattr(k8s_module.subprocess, "run", boom)
        assert ctrl.delete_k8s_resource({"kind": "Topology", "name": "t1"}) is False


# --- get_/delete_ wrappers not covered elsewhere ------------------------
class TestMoreByName:
    def test_get_deployment_by_name(self, ctrl):
        dep = MagicMock()
        dep.to_dict.return_value = {"metadata": {"name": "d1"}}
        dep.status.replicas = 2
        dep.status.ready_replicas = 2
        ctrl.apps_v1_api.read_namespaced_deployment = MagicMock(return_value=dep)
        result = ctrl.get_deployment_by_name({"name": "d1"})
        assert result["is_ok"] is True

    def test_get_config_map_by_name(self, ctrl):
        cfg = MagicMock()
        cfg.to_dict.return_value = {"metadata": {"name": "c1"}}
        ctrl.v1_api.read_namespaced_config_map = MagicMock(return_value=cfg)
        assert ctrl.get_config_map_by_name({"name": "c1"})["is_ok"] is True

    def test_get_resources_by_name_dispatches_all_kinds(self, ctrl):
        ctrl.get_pod_by_name = MagicMock(return_value={"kind": "Pod"})
        ctrl.get_service_by_name = MagicMock(return_value={"kind": "Service"})
        ctrl.get_deployment_by_name = MagicMock(return_value={"kind": "Deployment"})
        ctrl.get_config_map_by_name = MagicMock(return_value={"kind": "ConfigMap"})
        ctrl.get_k8s_resource = MagicMock(return_value={"kind": "Other"})
        results = ctrl.get_resources_by_name(
            [
                {"kind": "Pod", "name": "p"},
                {"kind": "Service", "name": "s"},
                {"kind": "Deployment", "name": "d"},
                {"kind": "ConfigMap", "name": "c"},
                {"kind": "Topology", "name": "t"},
            ]
        )
        assert [r["kind"] for r in results] == [
            "Pod",
            "Service",
            "Deployment",
            "ConfigMap",
            "Other",
        ]

    def test_delete_config_map_by_name(self, ctrl):
        ctrl.v1_api.delete_namespaced_config_map = MagicMock()
        assert ctrl.delete_config_map_by_name({"name": "c1"}) is True

    def test_delete_config_map_by_name_failure(self, ctrl):
        ctrl.v1_api.delete_namespaced_config_map = MagicMock(
            side_effect=RuntimeError("boom")
        )
        assert ctrl.delete_config_map_by_name({"name": "c1"}) is False
