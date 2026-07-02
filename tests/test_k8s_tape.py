"""Pytest for apps/controllers/kubernetes.py driven by a captured API tape.

A live Kubernetes API interaction was recorded with VCR.py in
``tests/data/k8s_tape.yaml`` (the full create-lab / inspect / delete
lifecycle). This suite reads the raw JSON response bodies embedded in that tape and
rebuilds them into real kubernetes client model objects (V1NodeList, V1PodList /
V1Pod, V1DeploymentList, V1ReplicaSetList, V1ServiceList, V1Deployment, V1Service)
via ``kubernetes.client.ApiClient.deserialize`` — the exact structure the API
returned. VCR.py itself is NOT used at run time: the tape is only a data source and
every Kubernetes call is replaced with ``unittest.mock.patch.object`` returning those
rebuilt objects.

Covers: get_statistics(), create_lab(), get_lab_resources(), delete_resources_by_name().

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_k8s_cluster_pods.py -v
"""

import importlib
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock, patch

import pytest
import yaml
from kubernetes import client as k8s_client
from kubernetes.client import Configuration

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

TAPE = os.path.join(os.path.dirname(__file__), "data", "k8s_tape.yaml")
NAMESPACE = "hackinsdn"


# --- rebuild the taped JSON into real kubernetes model objects ----------
class _FakeResp:
    """Minimal object exposing ``.data`` as ApiClient.deserialize expects."""

    def __init__(self, data):
        self.data = data


def _deserialize(json_string, response_type):
    """Turn a raw API JSON body into the matching kubernetes client model."""
    return k8s_client.ApiClient().deserialize(_FakeResp(json_string), response_type)


def _no_validation_config_init(self, *args, **kwargs):
    """Configuration.__init__ that disables client-side validation.

    The tape bodies are trimmed to only the fields the controller reads, so they
    omit fields the client marks "required" (e.g. V1ContainerStatus.image). Model
    objects are built with ``Configuration()`` internally, so we patch its __init__
    during deserialization to skip that validation. Validation is restored on exit.
    """
    _ORIG_CONFIG_INIT(self, *args, **kwargs)
    self.client_side_validation = False


_ORIG_CONFIG_INIT = Configuration.__init__


def _load_tape():
    """Read the VCR tape and rebuild each response body into a model object.

    Interactions are matched by request method + URI so the mapping stays correct
    regardless of ordering. VCR.py is not imported: the tape is plain YAML here.
    """
    with open(TAPE) as fh:
        interactions = yaml.safe_load(fh)["interactions"]

    def body(method, endpoint):
        for it in interactions:
            req = it["request"]
            if req["method"] != method:
                continue
            if req["uri"].split("?")[0].rstrip("/").endswith(endpoint):
                return it["response"]["body"]["string"]
        raise KeyError(f"{method} ...{endpoint} not found in tape")

    with patch.object(Configuration, "__init__", _no_validation_config_init):
        return {
            "node_list": _deserialize(body("GET", "/nodes"), "V1NodeList"),
            "created_deployment": _deserialize(
                body("POST", "/deployments"), "V1Deployment"
            ),
            "created_service": _deserialize(body("POST", "/services"), "V1Service"),
            "deployment_list": _deserialize(
                body("GET", "/deployments"), "V1DeploymentList"
            ),
            "replicaset_list": _deserialize(
                body("GET", "/replicasets"), "V1ReplicaSetList"
            ),
            "pod_list": _deserialize(body("GET", "/pods"), "V1PodList"),
            "service_list": _deserialize(body("GET", "/services"), "V1ServiceList"),
            "config_map_list": _deserialize(
                body("GET", "/configmaps"), "V1ConfigMapList"
            ),
        }


TAPE_OBJECTS = _load_tape()


# --- fixtures -----------------------------------------------------------
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
    """A fully-wired K8sController with the kube client patched out."""
    monkeypatch.setattr(k8s_module.app_config, "K8S_NAMESPACE", NAMESPACE)
    monkeypatch.setattr(k8s_module.config, "load_kube_config", lambda **k: None)
    monkeypatch.setattr(k8s_module.client, "CoreV1Api", lambda: MagicMock())
    monkeypatch.setattr(k8s_module.client, "AppsV1Api", lambda: MagicMock())
    monkeypatch.setattr(k8s_module.client, "ApiClient", lambda: MagicMock())
    controller = K8sController()
    controller.k8s_avoid_nodes = set()
    return controller


# --- get_statistics -----------------------------------------------------
class TestGetStatistics:
    def test_from_taped_node_list(self, ctrl):
        """get_statistics() over the single taped node yields its capacity totals."""
        ctrl.nodes_last_updated = 0  # bypass the 60s update_nodes cache
        with patch.object(
            ctrl.v1_api, "list_node", return_value=TAPE_OBJECTS["node_list"]
        ) as mock_list_node:
            stats = ctrl.get_statistics()

        mock_list_node.assert_called_once()
        # taped node k8s-testing: cpu=8, memory=16375548Ki, ephemeral-storage=102624184Ki,
        # pods=110, condition Ready=True -> exactly one ready node.
        assert stats == {
            "total_cpu_capacity": 8,
            "total_memory_capacity": "15.62 GB",
            "total_storage_capacity": "97.87 GB",
            "total_pods": 110,
            "total_nodes": 1,
        }


# --- create_lab ---------------------------------------------------------
class TestCreateLab:
    def test_creates_deployment_and_service(self, ctrl):
        """create_lab() applies each manifest doc via create_from_dict and reports
        the kind/name/uid of the objects the API returned (from the tape)."""
        manifest = yaml.dump_all(
            [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {"name": "helloworld-hackinsdn-18fb2637c46a47"},
                    "spec": {},
                },
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": "helloworld-hackinsdn-18fb2637c46a47"},
                    "spec": {},
                },
            ]
        )

        # create_from_dict is the kubernetes.utils helper the controller imports;
        # patch it to return the taped created objects instead of hitting the API.
        with patch.object(
            k8s_module,
            "create_from_dict",
            side_effect=[
                [TAPE_OBJECTS["created_deployment"]],
                [TAPE_OBJECTS["created_service"]],
            ],
        ) as mock_create:
            status, results = ctrl.create_lab(
                "aec2666f-b887-4e09-b628-7b14c7270016",
                manifest,
                user_uid="757bc0a552be49",
                pod_hash="18fb2637c46a47",
            )

        assert status is True
        assert mock_create.call_count == 2
        assert results == [
            {
                "kind": "Deployment",
                "name": "helloworld-hackinsdn-18fb2637c46a47",
                "uid": "8755b19e-1ccf-43d1-a277-a99981a542af",
            },
            {
                "kind": "Service",
                "name": "helloworld-hackinsdn-18fb2637c46a47",
                "uid": "755f9dba-bb5c-4cde-ad83-36f390ad27a5",
            },
        ]

    def test_dry_run_skips_creation(self, ctrl):
        """dry_run must not call create_from_dict at all."""
        manifest = yaml.dump(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "svc"},
                "spec": {},
            }
        )
        with patch.object(k8s_module, "create_from_dict") as mock_create:
            status, msg = ctrl.create_lab("lab", manifest, dry_run=True, user_uid="u")
        assert status is True
        mock_create.assert_not_called()


# --- get_lab_resources --------------------------------------------------
class TestGetLabResources:
    def test_links_pod_service_and_node_ip(self, ctrl):
        """Given the owning deployment+service uids, get_lab_resources() walks the
        deployment -> replicaset -> pod -> service chain from the tape and builds a
        NodePort URL using the node InternalIP."""
        ctrl.nodes_last_updated = 0
        # owner uids of the lab's deployment and service (as create_lab would return)
        resources = [
            {"uid": "8755b19e-1ccf-43d1-a277-a99981a542af"},  # deployment
            {"uid": "755f9dba-bb5c-4cde-ad83-36f390ad27a5"},  # service
        ]
        with patch.object(
            ctrl.apps_v1_api,
            "list_namespaced_deployment",
            return_value=TAPE_OBJECTS["deployment_list"],
        ), patch.object(
            ctrl.apps_v1_api,
            "list_namespaced_replica_set",
            return_value=TAPE_OBJECTS["replicaset_list"],
        ), patch.object(
            ctrl.v1_api, "list_namespaced_pod", return_value=TAPE_OBJECTS["pod_list"]
        ), patch.object(
            ctrl.v1_api,
            "list_namespaced_service",
            return_value=TAPE_OBJECTS["service_list"],
        ), patch.object(
            ctrl.v1_api, "list_node", return_value=TAPE_OBJECTS["node_list"]
        ):
            labs = ctrl.get_lab_resources(resources)

        # only the helloworld pod belongs to the requested owners (the mnsec-proxy
        # pod/service are unrelated and must be filtered out).
        assert len(labs) == 1
        lab = labs[0]
        assert lab["name"] == "helloworld-hackinsdn-18fb2637c46a47-755db7c785-dn9d9"
        assert lab["node_name"] == "k8s-testing"
        assert lab["containers"] == ["helloworld-hackinsdn"]
        # NodePort 30996 exposed via the node InternalIP (redacted in the tape)
        assert lab["services"] == [
            ["http-helloworld-webserver", "http://198.51.100.10:30996"]
        ]


# --- delete_resources_by_name -------------------------------------------
class TestDeleteResourcesByName:
    def test_dispatches_delete_to_correct_apis(self, ctrl):
        """delete_resources_by_name() routes each kind to the matching delete call."""
        ctrl.apps_v1_api.delete_namespaced_deployment = MagicMock()
        ctrl.v1_api.delete_namespaced_service = MagicMock()

        results = ctrl.delete_resources_by_name(
            [
                {"kind": "Deployment", "name": "helloworld-hackinsdn-18fb2637c46a47"},
                {"kind": "Service", "name": "helloworld-hackinsdn-18fb2637c46a47"},
            ]
        )

        assert results == [True, True]
        ctrl.apps_v1_api.delete_namespaced_deployment.assert_called_once_with(
            name="helloworld-hackinsdn-18fb2637c46a47", namespace=NAMESPACE
        )
        ctrl.v1_api.delete_namespaced_service.assert_called_once_with(
            name="helloworld-hackinsdn-18fb2637c46a47", namespace=NAMESPACE
        )

    def test_delete_failure_returns_false(self, ctrl):
        """An API error on delete is swallowed and reported as False."""
        ctrl.apps_v1_api.delete_namespaced_deployment = MagicMock(
            side_effect=RuntimeError("boom")
        )
        results = ctrl.delete_resources_by_name(
            [{"kind": "Deployment", "name": "helloworld-hackinsdn-18fb2637c46a47"}]
        )
        assert results == [False]


# --- list_pods ----------------------------------------------------------
class TestListPods:
    def test_summarizes_taped_pods(self, ctrl):
        """list_pods() flattens the taped PodList into one summary dict per pod."""
        with patch.object(
            ctrl.v1_api, "list_namespaced_pod", return_value=TAPE_OBJECTS["pod_list"]
        ) as mock_list:
            pods = ctrl.list_pods()

        mock_list.assert_called_once_with(namespace=NAMESPACE)
        assert len(pods) == 2
        by_name = {p["name"]: p for p in pods}

        # helloworld pod: still Pending, its single container not yet ready, no IP.
        hello = by_name["helloworld-hackinsdn-18fb2637c46a47-755db7c785-dn9d9"]
        assert hello["uid"] == "10e078e9-acd1-4347-bdfc-33626bc0c7b8"
        assert hello["node_name"] == "k8s-testing"
        assert hello["pod_ip"] is None
        assert hello["phase"] == "Pending"
        assert hello["containers_total"] == 1
        assert hello["containers_ready"] == 0
        assert hello["containers"] == ["helloworld-hackinsdn"]

        # mnsec-proxy pod: Running with all 3 containers ready and an assigned IP.
        proxy = by_name["mnsec-proxy-5ddbcfb656-6ct5n"]
        assert proxy["phase"] == "Running"
        assert proxy["pod_ip"] == "10.97.108.27"
        assert proxy["containers_total"] == 3
        assert proxy["containers_ready"] == 3
        assert proxy["containers"] == ["nginx", "auth-service", "kubectl-service"]

        # every entry carries a human age string and a yaml `more` dump of itself.
        for pod in pods:
            assert isinstance(pod["age"], str)
            assert pod["name"] in pod["more"]


# --- list_deployments ---------------------------------------------------
class TestListDeployments:
    def test_summarizes_taped_deployments(self, ctrl):
        """list_deployments() reports replica counts and container names."""
        with patch.object(
            ctrl.apps_v1_api,
            "list_namespaced_deployment",
            return_value=TAPE_OBJECTS["deployment_list"],
        ) as mock_list:
            deployments = ctrl.list_deployments()

        mock_list.assert_called_once_with(namespace=NAMESPACE)
        assert len(deployments) == 2
        by_name = {d["name"]: d for d in deployments}

        # helloworld: 1 desired replica, none ready yet -> ready_replicas None => 0.
        hello = by_name["helloworld-hackinsdn-18fb2637c46a47"]
        assert hello["uid"] == "8755b19e-1ccf-43d1-a277-a99981a542af"
        assert hello["containers_total"] == 1
        assert hello["containers_ready"] == 0
        assert hello["containers"] == "helloworld-hackinsdn"

        # mnsec-proxy: fully ready, three containers joined into a string.
        proxy = by_name["mnsec-proxy"]
        assert proxy["containers_total"] == 1
        assert proxy["containers_ready"] == 1
        assert proxy["containers"] == "nginx,auth-service,kubectl-service"

        for dep in deployments:
            assert isinstance(dep["age"], str)
            assert dep["name"] in dep["more"]


# --- list_services ------------------------------------------------------
class TestListServices:
    def test_summarizes_taped_services(self, ctrl):
        """list_services() formats ports as target:node/proto and reports the type."""
        with patch.object(
            ctrl.v1_api,
            "list_namespaced_service",
            return_value=TAPE_OBJECTS["service_list"],
        ) as mock_list:
            services = ctrl.list_services()

        mock_list.assert_called_once_with(namespace=NAMESPACE)
        assert len(services) == 2
        by_name = {s["name"]: s for s in services}

        # helloworld: NodePort exposing 80 -> 30996.
        hello = by_name["helloworld-hackinsdn-18fb2637c46a47"]
        assert hello["uid"] == "755f9dba-bb5c-4cde-ad83-36f390ad27a5"
        assert hello["type"] == "NodePort"
        assert hello["ports"] == "80:30996/TCP"

        # mnsec-proxy-service: ClusterIP has no node port (None).
        proxy = by_name["mnsec-proxy-service"]
        assert proxy["type"] == "ClusterIP"
        assert proxy["ports"] == "443:None/TCP"

        for srv in services:
            assert isinstance(srv["age"], str)
            assert srv["name"] in srv["more"]


# --- get_labs_by_user ---------------------------------------------------
LAB_ID = "aec2666f-b887-4e09-b628-7b14c7270016"
USER_UID = "757bc0a552be49"


class TestGetLabsByUser:
    def test_groups_resources_by_lab_and_user(self, ctrl):
        """get_labs_by_user() gathers the deployment, pod, service and config map
        that share the (lab_id, user_uid) labels, linking pod NodePorts to the
        node IP and skipping resources without those labels (mnsec-proxy)."""
        ctrl.nodes_last_updated = 0
        with patch.object(
            ctrl.apps_v1_api,
            "list_namespaced_deployment",
            return_value=TAPE_OBJECTS["deployment_list"],
        ), patch.object(
            ctrl.apps_v1_api,
            "list_namespaced_replica_set",
            return_value=TAPE_OBJECTS["replicaset_list"],
        ), patch.object(
            ctrl.v1_api, "list_namespaced_pod", return_value=TAPE_OBJECTS["pod_list"]
        ), patch.object(
            ctrl.v1_api,
            "list_namespaced_service",
            return_value=TAPE_OBJECTS["service_list"],
        ), patch.object(
            ctrl.v1_api,
            "list_namespaced_config_map",
            return_value=TAPE_OBJECTS["config_map_list"],
        ), patch.object(
            ctrl.v1_api, "list_node", return_value=TAPE_OBJECTS["node_list"]
        ):
            labs = ctrl.get_labs_by_user(USER_UID)

        # exactly one (lab_id, user_uid) group; mnsec-proxy resources are unlabeled.
        assert list(labs.keys()) == [(LAB_ID, USER_UID)]
        entries = labs[(LAB_ID, USER_UID)]
        by_kind = {e["kind"]: e for e in entries}
        assert set(by_kind) == {"deployment", "pod", "service", "config_map"}

        assert by_kind["deployment"]["name"] == "helloworld-hackinsdn-18fb2637c46a47"
        assert by_kind["deployment"]["containers"] == ["helloworld-hackinsdn"]

        pod = by_kind["pod"]
        assert pod["phase"] == "Pending"
        assert pod["node_name"] == "k8s-testing"
        assert pod["services"] == [
            ["http-helloworld-webserver", "http://198.51.100.10:30996"]
        ]

        service = by_kind["service"]
        assert service["ports"] == ["80:30996/TCP"]
        assert service["node_ip"] == "198.51.100.10"
        assert service["links"] == [
            ["http-helloworld-webserver", "http://198.51.100.10:30996"]
        ]

        # config map: containers_total/ready reflect the number of data keys.
        cfg = by_kind["config_map"]
        assert cfg["name"] == "helloworld-config"
        assert cfg["containers_total"] == 2

    def test_returns_empty_without_api(self, ctrl):
        """No configured API client -> an empty list, no calls attempted."""
        ctrl.v1_api = None
        assert ctrl.get_labs_by_user(USER_UID) == []
