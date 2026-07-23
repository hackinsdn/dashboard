"""Pytest suite for the multi-kubeconfig failover in apps/controllers/kubernetes.py.

Builds a K8sController wired to two (or three) fake kubeconfig "endpoints",
each with its own mocked kubernetes client, and drives the @with_failover
machinery: connectivity/auth failures rotate to the next kubeconfig, valid API
errors do not, the choice is sticky and circular, kubectl calls fail over too,
and the failover counters / get_statistics() block are populated.

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_k8s_failover.py -v
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import types
from types import SimpleNamespace
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

k8s_module = importlib.import_module("apps.controllers.kubernetes")
K8sController = k8s_module.K8sController
ApiException = k8s_module.ApiException

flask_app.config["TESTING"] = True


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
def make_ctrl(monkeypatch):
    """Factory building a controller with N fake, fully-built kubeconfigs."""

    def _make(paths):
        monkeypatch.setattr(k8s_module.app_config, "K8S_NAMESPACE", "test-ns")
        monkeypatch.setattr(k8s_module.app_config, "K8S_REQUEST_TIMEOUT", 7.0)
        monkeypatch.setattr(k8s_module.app_config, "K8S_CONFIGS", list(paths))
        monkeypatch.setattr(k8s_module.app_config, "K8S_FLAP_THRESHOLD", 3)
        monkeypatch.setattr(k8s_module.config, "load_kube_config", lambda **k: None)
        monkeypatch.setattr(k8s_module.client, "Configuration", lambda *a, **k: MagicMock())
        monkeypatch.setattr(k8s_module.client, "ApiClient", lambda *a, **k: MagicMock())
        monkeypatch.setattr(k8s_module.client, "CoreV1Api", lambda *a, **k: MagicMock())
        monkeypatch.setattr(k8s_module.client, "AppsV1Api", lambda *a, **k: MagicMock())
        monkeypatch.setattr(k8s_module.client, "DiscoveryV1Api", lambda *a, **k: MagicMock())
        ctrl = K8sController()
        # materialise each endpoint's (distinct) mock clients up front
        for ep in ctrl._endpoints:
            ep.ensure_built(ctrl._lock)
        return ctrl

    return _make


def _fake_pod(phase="Running"):
    pod = MagicMock()
    pod.to_dict.return_value = {}
    pod.status.phase = phase
    return pod


# --- rotation / stickiness ----------------------------------------------
class TestRotation:
    def test_failover_on_timeout(self, make_ctrl):
        ctrl = make_ctrl(["/a", "/b"])
        ctrl._endpoints[0].v1_api.read_namespaced_pod.side_effect = TimeoutError("stalled")
        ctrl._endpoints[1].v1_api.read_namespaced_pod.return_value = _fake_pod()

        result = ctrl.get_pod_by_name({"name": "p1"})

        assert result["is_ok"] is True
        assert ctrl._active_idx == 1  # moved to the working kubeconfig
        assert ctrl._failover_stats["total_failovers"] == 1

    def test_sticky_stays_on_working_endpoint(self, make_ctrl):
        ctrl = make_ctrl(["/a", "/b"])
        ctrl._endpoints[0].v1_api.read_namespaced_pod.side_effect = ConnectionError("down")
        ctrl._endpoints[1].v1_api.read_namespaced_pod.return_value = _fake_pod()

        ctrl.get_pod_by_name({"name": "p1"})  # fails over 0 -> 1
        ctrl._endpoints[0].v1_api.read_namespaced_pod.reset_mock()
        ctrl.get_pod_by_name({"name": "p2"})  # should use 1 directly

        assert ctrl._active_idx == 1
        ctrl._endpoints[0].v1_api.read_namespaced_pod.assert_not_called()

    def test_circle_wraps_around(self, make_ctrl):
        ctrl = make_ctrl(["/a", "/b"])
        # start on 1 (as if a previous failover happened)
        ctrl._active_idx = 1
        ctrl._endpoints[1].v1_api.read_namespaced_pod.side_effect = TimeoutError("stalled")
        ctrl._endpoints[0].v1_api.read_namespaced_pod.return_value = _fake_pod()

        result = ctrl.get_pod_by_name({"name": "p1"})

        assert result["is_ok"] is True
        assert ctrl._active_idx == 0  # wrapped 1 -> 0

    def test_auth_401_and_403_trigger_failover(self, make_ctrl):
        for status in (401, 403):
            ctrl = make_ctrl(["/a", "/b"])
            ctrl._endpoints[0].v1_api.read_namespaced_pod.side_effect = ApiException(status=status)
            ctrl._endpoints[1].v1_api.read_namespaced_pod.return_value = _fake_pod()

            result = ctrl.get_pod_by_name({"name": "p1"})

            assert result["is_ok"] is True
            assert ctrl._active_idx == 1
            assert ctrl._failover_stats["recent"][-1]["reason"] == f"auth_{status}"

    def test_no_failover_on_valid_api_error(self, make_ctrl):
        ctrl = make_ctrl(["/a", "/b"])
        ctrl._endpoints[0].v1_api.read_namespaced_pod.side_effect = ApiException(status=404)

        with pytest.raises(ApiException):
            ctrl.get_pod_by_name({"name": "missing"})

        assert ctrl._active_idx == 0  # unchanged
        assert ctrl._failover_stats["total_failovers"] == 0
        ctrl._endpoints[1].v1_api.read_namespaced_pod.assert_not_called()

    def test_all_endpoints_down_reraises_after_len_attempts(self, make_ctrl):
        ctrl = make_ctrl(["/a", "/b", "/c"])
        for ep in ctrl._endpoints:
            ep.v1_api.read_namespaced_pod.side_effect = TimeoutError("down")

        with pytest.raises(TimeoutError):
            ctrl.get_pod_by_name({"name": "p1"})

        # exactly one attempt per endpoint
        assert ctrl._failover_stats["total_failovers"] == 3

    def test_all_auth_fail_does_not_loop_forever(self, make_ctrl):
        ctrl = make_ctrl(["/a", "/b"])
        for ep in ctrl._endpoints:
            ep.v1_api.read_namespaced_pod.side_effect = ApiException(status=401)

        with pytest.raises(ApiException):
            ctrl.get_pod_by_name({"name": "p1"})
        assert ctrl._failover_stats["total_failovers"] == 2


# --- concurrency (compare-and-swap rotation) ----------------------------
class TestThunderingHerd:
    def test_rotate_from_advances_exactly_once(self, make_ctrl):
        ctrl = make_ctrl(["/a", "/b", "/c"])
        ep0 = ctrl._endpoints[0]

        ctrl._rotate_from(ep0)  # 0 -> 1
        assert ctrl._active_idx == 1
        # a second greenlet that also observed ep0 failing must NOT advance again
        ctrl._rotate_from(ep0)
        assert ctrl._active_idx == 1


# --- kubectl failover ----------------------------------------------------
class TestKubectlFailover:
    def test_get_k8s_resource_fails_over(self, make_ctrl, monkeypatch):
        ctrl = make_ctrl(["/a", "/b"])

        class _Completed:
            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(cmd, *a, **k):
            if "--kubeconfig" in cmd and "/a" in cmd:
                raise subprocess.TimeoutExpired(cmd="kubectl", timeout=7)
            return _Completed(json.dumps({"kind": "Foo", "metadata": {"name": "f1"}}))

        monkeypatch.setattr(k8s_module.subprocess, "run", fake_run)
        result = ctrl.get_k8s_resource({"kind": "Foo", "name": "f1"})

        assert result["is_ok"] is True
        assert ctrl._active_idx == 1

    def test_kubectl_notfound_propagates_without_failover(self, make_ctrl, monkeypatch):
        ctrl = make_ctrl(["/a", "/b"])

        def fake_run(cmd, *a, **k):
            raise subprocess.CalledProcessError(1, cmd, stderr='Error: resource "x" not found')

        monkeypatch.setattr(k8s_module.subprocess, "run", fake_run)
        with pytest.raises(Exception, match="Failed to get k8s resource"):
            ctrl.get_k8s_resource({"kind": "Foo", "name": "f1"})
        assert ctrl._active_idx == 0  # a valid "not found" must not rotate

    def test_kubectl_unauthorized_fails_over(self, make_ctrl, monkeypatch):
        ctrl = make_ctrl(["/a", "/b"])

        class _Completed:
            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(cmd, *a, **k):
            if "--kubeconfig" in cmd and "/a" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="error: You must be logged in to the server (Unauthorized)")
            return _Completed(json.dumps({"kind": "Foo", "metadata": {"name": "f1"}}))

        monkeypatch.setattr(k8s_module.subprocess, "run", fake_run)
        result = ctrl.get_k8s_resource({"kind": "Foo", "name": "f1"})
        assert result["is_ok"] is True
        assert ctrl._active_idx == 1


# --- metrics / observability --------------------------------------------
class TestMetrics:
    def test_get_failover_stats_none_for_single_config(self, make_ctrl):
        ctrl = make_ctrl(["/only"])
        assert ctrl.get_failover_stats() is None

    def test_statistics_has_no_kubeconfig_block_for_single_config(self, make_ctrl):
        ctrl = make_ctrl(["/only"])
        ctrl._endpoints[0].v1_api.list_node.return_value = SimpleNamespace(items=[])
        assert "kubeconfig" not in ctrl.get_statistics()

    def test_statistics_reports_failover_block(self, make_ctrl):
        ctrl = make_ctrl(["/a", "/b"])
        ctrl._endpoints[0].v1_api.read_namespaced_pod.side_effect = TimeoutError("down")
        ctrl._endpoints[1].v1_api.read_namespaced_pod.return_value = _fake_pod()
        ctrl._endpoints[0].v1_api.list_node.return_value = SimpleNamespace(items=[])
        ctrl._endpoints[1].v1_api.list_node.return_value = SimpleNamespace(items=[])

        ctrl.get_pod_by_name({"name": "p1"})  # one failover 0 -> 1
        stats = ctrl.get_statistics()

        assert "kubeconfig" in stats
        block = stats["kubeconfig"]
        assert block["active"] == "/b"
        assert block["total_failovers"] == 1
        assert block["endpoints"][0]["failovers"] == 1
        assert block["endpoints"][1]["active"] is True

    def test_flapping_is_edge_triggered(self, make_ctrl):
        ctrl = make_ctrl(["/a", "/b"])
        ep0 = ctrl._endpoints[0]
        # three failovers within the window crosses the default threshold of 3
        for _ in range(3):
            ctrl._record_failover(ep0, TimeoutError("down"))

        assert ctrl._flap_logged is True
        assert ctrl.get_failover_stats()["is_flapping"] is True


# --- backward compatibility ---------------------------------------------
class TestBackwardCompat:
    def test_single_config_never_fails_over(self, make_ctrl):
        ctrl = make_ctrl(["/only"])
        ctrl._endpoints[0].v1_api.read_namespaced_pod.side_effect = ApiException(status=500)
        # only one endpoint -> one attempt, then the error propagates
        with pytest.raises(ApiException):
            ctrl.get_pod_by_name({"name": "p1"})
        assert ctrl._active_idx == 0

    def test_disabled_controller_returns_none_clients(self, monkeypatch):
        monkeypatch.setattr(k8s_module.app_config, "K8S_NAMESPACE", "")
        ctrl = K8sController()
        assert ctrl._endpoints == []
        assert ctrl.v1_api is None
        assert ctrl.get_labs_by_user("u1") == []
