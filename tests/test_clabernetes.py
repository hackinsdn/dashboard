"""Pytest suite for the C9sController (ContainerLab topology processing).

Unlike the route suites, apps/controllers/clabernetes.py is pure logic (stdlib +
yaml, no apps imports) and needs no Flask app or database - so this is a plain
unit-test file. The only wrinkle is that the module raises at *import* time when
the `clabverter` binary is missing (which is why every other suite stubs it out).
We load the module directly from its file with `os.path.exists`/`os.access`
patched so the guard passes; loading by path sidesteps apps/controllers/__init__
(which eagerly imports the real k8s/git controllers) and never touches
sys.modules["apps.controllers.clabernetes"], so it can't disturb the shared stub
the other suites rely on. `subprocess` is mocked for convert_clab.

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_clabernetes.py -v
"""
import importlib.util
import os
import subprocess
import sys
import types

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(REPO_ROOT, "apps", "controllers", "clabernetes.py")
_CLABVERTER_BIN = "/usr/local/bin/clabverter"

# --- import the module past its clabverter binary guard ---------------------
_real_exists, _real_access = os.path.exists, os.access
os.path.exists = lambda p, *a, **k: True if p == _CLABVERTER_BIN else _real_exists(p, *a, **k)
os.access = lambda p, m, *a, **k: True if p == _CLABVERTER_BIN else _real_access(p, m, *a, **k)
try:
    _spec = importlib.util.spec_from_file_location("clabernetes_under_test", _PATH)
    c9s_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(c9s_mod)
finally:
    os.path.exists, os.access = _real_exists, _real_access

C9sController = c9s_mod.C9sController


# --- fixtures / helpers -------------------------------------------------
@pytest.fixture()
def c9s():
    return C9sController()


def write_topology(directory, topology, filename="test.clab.yaml"):
    path = os.path.join(directory, filename)
    with open(path, "w") as f:
        yaml.dump(topology, f)
    return path


def valid_topology():
    return {
        "name": "mylab",
        "topology": {
            "nodes": {
                "r1": {
                    "kind": "linux",
                    "ports": ["8080:80", "53/udp"],
                    "startup-config": "configs/r1.cfg",
                    "binds": ["configs/r1.cfg:/etc/config"],
                    "imagePullSecrets": ["myreg"],
                }
            }
        },
    }


# --- filename_from_uploads ----------------------------------------------
class TestFilenameFromUploads:
    def test_resolves_to_uploaded_basename(self, c9s):
        assert c9s.filename_from_uploads("configs/r1.cfg", ["r1.cfg", "other.txt"]) == "r1.cfg"

    def test_returns_original_when_no_match(self, c9s):
        assert c9s.filename_from_uploads("startup.cfg", ["r1.cfg"]) == "startup.cfg"


# --- clean_up_for_clab_graph --------------------------------------------
class TestCleanUpForClabGraph:
    def test_removes_binds_everywhere(self, c9s):
        topology = {
            "topology": {
                "defaults": {"binds": ["a:b"], "image": "x"},
                "kinds": {"linux": {"binds": ["c:d"]}},
                "groups": {"g1": {"binds": ["e:f"]}},
                "nodes": {"n1": {"binds": ["g:h"], "kind": "linux"}},
            }
        }
        result = c9s.clean_up_for_clab_graph(topology)
        assert "binds" not in result["topology"]["defaults"]
        assert "binds" not in result["topology"]["kinds"]["linux"]
        assert "binds" not in result["topology"]["groups"]["g1"]
        assert "binds" not in result["topology"]["nodes"]["n1"]
        assert result["topology"]["defaults"]["image"] == "x"  # untouched

    def test_minimal_topology_without_optional_blocks(self, c9s):
        topology = {"topology": {"nodes": {"n1": {"binds": ["x:y"]}}}}
        result = c9s.clean_up_for_clab_graph(topology)
        assert "binds" not in result["topology"]["nodes"]["n1"]


# --- check_topology_secrets ---------------------------------------------
class TestCheckTopologySecrets:
    def test_all_known_secrets_are_rewritten(self, c9s):
        topology = {"topology": {"nodes": {"n1": {"imagePullSecrets": ["s1"]}}}}
        failed = c9s.check_topology_secrets(topology, {"s1": "ks1"})
        assert failed == []
        assert topology["topology"]["nodes"]["n1"]["imagePullSecrets"] == ["ks1"]

    def test_unknown_secret_is_reported(self, c9s):
        topology = {"topology": {"nodes": {"n1": {"imagePullSecrets": ["s1", "s2"]}}}}
        failed = c9s.check_topology_secrets(topology, {"s1": "ks1"})
        assert len(failed) == 1
        assert "s2" in failed[0]
        # known one still rewritten, unknown one left as-is
        assert topology["topology"]["nodes"]["n1"]["imagePullSecrets"] == ["ks1", "s2"]

    def test_defaults_and_kinds_are_covered(self, c9s):
        topology = {
            "topology": {
                "nodes": {},
                "defaults": {"imagePullSecrets": ["d1"]},
                "kinds": {"linux": {"imagePullSecrets": ["k1"]}},
            }
        }
        failed = c9s.check_topology_secrets(topology, {"d1": "kd1", "k1": "kk1"})
        assert failed == []
        assert topology["topology"]["defaults"]["imagePullSecrets"] == ["kd1"]
        assert topology["topology"]["kinds"]["linux"]["imagePullSecrets"] == ["kk1"]


# --- process_clab_topology ----------------------------------------------
class TestProcessClabTopology:
    def test_no_topology_file(self, c9s, tmp_path):
        status, msg = c9s.process_clab_topology(str(tmp_path), clab_uuid="abc")
        assert status is False
        assert "No ContainerLab topology file" in msg

    def test_too_many_topology_files(self, c9s, tmp_path):
        write_topology(str(tmp_path), valid_topology(), filename="a.clab.yaml")
        write_topology(str(tmp_path), valid_topology(), filename="b.clab.yaml")
        status, msg = c9s.process_clab_topology(str(tmp_path), clab_uuid="abc")
        assert status is False
        assert "Too many ContainerLab topology files" in msg

    def test_invalid_topology_content(self, c9s, tmp_path):
        # a file with no "topology" key
        write_topology(str(tmp_path), {"name": "x"}, filename="bad.clab.yaml")
        status, msg = c9s.process_clab_topology(str(tmp_path), clab_uuid="abc")
        assert status is False
        assert "Failed to load ContainerLab topology" in msg

    def test_happy_path(self, c9s, tmp_path):
        write_topology(str(tmp_path), valid_topology())
        # an uploaded file that the startup-config/binds paths resolve to
        with open(os.path.join(str(tmp_path), "r1.cfg"), "w") as f:
            f.write("hostname r1\n")

        status, topology = c9s.process_clab_topology(
            str(tmp_path), clab_uuid="abc", secrets={"myreg": "clab-secret-x"}
        )
        assert status is True
        assert topology["name"] == "clab-abc"
        assert topology["ports"] == {"r1": {"80": "8080:80", "53": "53/udp"}}
        assert topology["topology"]["nodes"]["r1"]["imagePullSecrets"] == ["clab-secret-x"]
        assert topology["topology"]["nodes"]["r1"]["startup-config"] == "r1.cfg"
        # binds are stripped from the returned (visualizer) topology
        assert "binds" not in topology["topology"]["nodes"]["r1"]
        # file was renamed to the uuid form; the original was removed
        assert os.path.exists(os.path.join(str(tmp_path), "clab-abc.clab.yaml"))
        assert not os.path.exists(os.path.join(str(tmp_path), "test.clab.yaml"))

    def test_missing_secret_fails(self, c9s, tmp_path):
        write_topology(str(tmp_path), valid_topology())
        status, msg = c9s.process_clab_topology(str(tmp_path), clab_uuid="abc", secrets={})
        assert status is False
        assert "Failed to find imagePullSecrets" in msg


# --- convert_clab (subprocess mocked) -----------------------------------
def _fake_subprocess(run):
    return types.SimpleNamespace(run=run, CalledProcessError=subprocess.CalledProcessError)


class TestConvertClab:
    def test_success_strips_namespace(self, c9s, monkeypatch):
        stdout = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm1\n"
            "---\n"
            "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: clab-ns\n"
            "---\n"
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: d1\n"
        )
        monkeypatch.setattr(
            c9s_mod, "subprocess",
            _fake_subprocess(lambda *a, **k: types.SimpleNamespace(stdout=stdout, returncode=0)),
        )
        status, result = c9s.convert_clab("/tmp/whatever", destination_namespace="ns")
        assert status is True
        assert "kind: Namespace" not in result
        assert "kind: ConfigMap" in result
        assert "kind: Deployment" in result

    def test_no_namespace_component(self, c9s, monkeypatch):
        stdout = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm1\n"
        monkeypatch.setattr(
            c9s_mod, "subprocess",
            _fake_subprocess(lambda *a, **k: types.SimpleNamespace(stdout=stdout, returncode=0)),
        )
        status, result = c9s.convert_clab("/tmp/whatever", destination_namespace="ns")
        assert status is False
        assert "could not find Namespace component" in result

    def test_called_process_error(self, c9s, monkeypatch):
        def boom(*a, **k):
            raise subprocess.CalledProcessError(returncode=1, cmd=["clabverter"], stderr="clabverter exploded")
        monkeypatch.setattr(c9s_mod, "subprocess", _fake_subprocess(boom))
        status, result = c9s.convert_clab("/tmp/whatever", destination_namespace="ns")
        assert status is False
        assert "clabverter exploded" in result

    def test_generic_exception(self, c9s, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("kaboom")
        monkeypatch.setattr(c9s_mod, "subprocess", _fake_subprocess(boom))
        status, result = c9s.convert_clab("/tmp/whatever", destination_namespace="ns")
        assert status is False
        assert "kaboom" in result


# --- get_topology_visualizer_manifest -----------------------------------
class TestVisualizerManifest:
    def test_substitutes_uuid_and_embeds_topology(self, c9s):
        topology = {"name": "clab-myid", "topology": {"nodes": {"n1": {"kind": "linux"}}}}
        manifest = c9s.get_topology_visualizer_manifest("myid", topology)
        assert "%CLAB_UUID%" not in manifest
        assert "topo-viewer-clab-myid" in manifest
        assert "kind: Deployment" in manifest
        assert "kind: Service" in manifest
        assert "kind: ConfigMap" in manifest
        # the topology is embedded in the ConfigMap's data
        assert "topology-data-clab-myid" in manifest
        assert "clab-myid" in manifest
