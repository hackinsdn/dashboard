# -*- coding: utf-8 -*-
import os
import subprocess
import yaml
import glob
import re


CLABVERTER_BIN = "/usr/local/bin/clabverter"

if not os.path.exists(CLABVERTER_BIN) or not os.access(CLABVERTER_BIN, os.X_OK):
    raise ValueError("Missing executable 'clabverter'. Please install it from https://github.com/srl-labs/clabernetes/releases/latest")

# PORT_RE: regex to match ports published on ContainerLab Topology
# CLab node ports are similar to Docker ports and accept the following cases:
# ["8080:80", "80", "192.168.1.100:8080:80", "8080:80/udp", "8080:80/tcp",
#  "[::1]:8080:80", "127.0.0.1:80/tcp", "[2001:db8:100:200::1]:80",
#  "127.0.0.1:80:8080/tcp", "8080/tcp"]
PORT_RE = re.compile(r"(\d+.\d+.\d+.\d+:|\[[0-9a-fA-F:]+\]:)?(\d+:)?(?P<port>\d+)(/\w+)?")

TOPO_VIEW_DEPLOYMENT="""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: topo-viewer-clab-%CLAB_UUID%
  labels:
    app: topo-viewer-clab-%CLAB_UUID%
spec:
  replicas: 1
  selector:
    matchLabels:
      app: topo-viewer-clab-%CLAB_UUID%
  template:
    metadata:
      name: topo-viewer-clab-%CLAB_UUID%
      labels:
        app: topo-viewer-clab-%CLAB_UUID%
        hackinsdn/displayName: topology-visualizer
    spec:
      containers:
      - name: topology-visualizer
        image: ghcr.io/srl-labs/clabernetes/clabernetes-launcher:latest
        ports:
        - containerPort: 50080
        command: ["sh", "-c"]
        args:
        - |
          service docker start
          until [ -s /topology-data.yaml ]; do sleep 1; done
          cat /topology-data.yaml
          clab graph --offline --topo /topology-data.yaml
        volumeMounts:
        - name: clab-topology-data
          mountPath: /topology-data.yaml
          readOnly: true
          subPath: topology-data.yaml
        securityContext:
          privileged: true
      volumes:
      - name: clab-topology-data
        configMap:
          defaultMode: 0640
          name: topology-data-clab-%CLAB_UUID%
"""
TOPO_VIEW_SERVICE="""
apiVersion: v1
kind: Service
metadata:
  name: topo-viewer-clab-%CLAB_UUID%
  labels:
    app: topo-viewer-clab-%CLAB_UUID%
spec:
  type: NodePort
  ports:
  - port: 50080
    targetPort: 50080
    name: http-topology-visualizer
  selector:
    app: topo-viewer-clab-%CLAB_UUID%
"""


class C9sController:
    """Controller to manage ContainerLab"""

    def __init__(self):
        pass

    def filename_from_uploads(self, filename, uploaded_files):
        """Try to get the correct filename from uploaded files."""
        for file in uploaded_files:
            if filename.endswith(f"/{file}"):
                return file
        return filename

    def process_clab_topology(self, topology_dir, clab_uuid=None, secrets={}):
        """Process ContainerLab topology before running clabverter.

        As part of the processing phase, we execute the following steps:
        - Basic validation on loading the file name. We expect to have a file named '*.clab.y*ml'
        - Change topology name to avoid invalid characters (it will become subdomain in Kubernetes)
        - Extract published ports
        - Handling absolute paths for startup-config and binds
        - Rename imagePullSecrets to kubernetes name to make sure they are unique and correct
        - Rename the topology file into clab_uuid to guarantee uniqueness
        """
        clab_files = glob.glob(f"{topology_dir}/*.clab.y*ml")
        if len(clab_files) != 1:
            msg = "No ContainerLab topology file!" if not clab_files else "Too many ContainerLab topology files!"
            return False, msg + " You must provide one file named *.clab.yaml"
        try:
            with open(clab_files[0], 'r') as f:
                topology = yaml.safe_load(f)
            assert "topology" in topology
            assert len(topology["topology"]["nodes"]) > 0
        except Exception as exc:
            return False, f"Failed to load ContainerLab topology: {exc}"

        uploaded_files = glob.glob("**/*", recursive=True, root_dir=topology_dir)

        # Change topology name to avoid invalid characters
        topology["name"] = f"clab-{clab_uuid}"

        # Handle absolute paths, extract ports and parse topology for visualizer
        published_ports = {}
        for node in topology["topology"]["nodes"].values():
            if node.get("startup-config"):
                node["startup-config"] = self.filename_from_uploads(node["startup-config"], uploaded_files)
            for i, bind in enumerate(node.get("binds", [])):
                bind_opts = bind.split(":")
                filename = self.filename_from_uploads(bind_opts[0], uploaded_files)
                if bind_opts[0] == filename:
                    continue
                if len(bind_opts) == 1:
                    # in case we only have one filename, the mount point should be preserved
                    bind_opts.append(bind_opts[0])
                bind_opts[0] = filename
                node["binds"][i] = ":".join(bind_opts)
            # extract ports
            for port in node.get("ports", []):
                if match := PORT_RE.match(port):
                    published_ports.setdefault(node_name, {})
                    published_ports[node_name][match.group("port")] = port

        # TODO: try to handle topology.kinds and topology.defaults if we have that file in the uploaded_files or one common absolute path

        topology["ports"] = published_ports

        # Handle imagePullSecrets
        failed_secrets = []
        for node_name, node in topology["topology"]["nodes"].items():
            for s_idx, s_name in enumerate(node.get("imagePullSecrets", [])):
                if s_name in secrets:
                    node["imagePullSecrets"][s_idx] = secrets[s_name]
                else:
                    failed_secrets.append(f"imagePullSecrets '{s_name}' for node={node_name} not defined!")
        for s_idx, s_name in enumerate(topology["topology"].get("defaults", {}).get("imagePullSecrets", [])):
            if s_name in secrets:
                topology["topology"]["defaults"]["imagePullSecrets"][s_idx] = secrets[s_name]
            else:
                failed_secrets.append(f"imagePullSecrets '{s_name}' for topology.defaults not defined!")
        for kind_name, kind in topology["topology"].get("kinds", {}).items():
            for s_idx, s_name in enumerate(kind.get("imagePullSecrets", [])):
                if s_name in secrets:
                    kind["imagePullSecrets"][s_idx] = secrets[s_name]
                else:
                    failed_secrets.append(f"imagePullSecrets '{s_name}' for kind={kind_name} not defined!")
        if failed_secrets:
            return False, f"Failed to find imagePullSecrets for topology: {failed_secrets}"

        # Force renaming the topology file with clab_uuid to guarantee uniqueness
        try:
            with open(f"{topology_dir}/clab-{clab_uuid}.clab.yaml", 'w') as file:
                yaml.dump(topology, file)
            os.unlink(clab_files[0])
        except Exception as e:
            return False, f"An error occurred saving ContainerLab topology file: {e}"

        # remove binds from topology to be consumed by visualizer
        if "defaults" in topology["topology"]:
            topology["topology"]["defaults"].pop("binds", None)
        for kind in topology["topology"].get("kinds", {}).values():
            kind.pop("binds", None)
        for group in topology["topology"].get("groups", {}).values():
            group.pop("binds", None)
        for node in topology["topology"]["nodes"].values():
            node.pop("binds", None)

        return True, topology

    def convert_clab(self, topology_dir, destination_namespace=None):
        """Convert the container lab topology into a kubernetes manifest."""
        try:
            result = subprocess.run(
                [CLABVERTER_BIN, "--stdout", "--quiet", "--destinationNamespace", destination_namespace],
                cwd=topology_dir,
                capture_output=True,
                text=True,
                check=True
            )
        except subprocess.CalledProcessError as exc:
            return False, f"Convert ContainerLab failed: {exc.stderr}"
        except Exception as exc:
            return False, f"Convert ContainerLab failed: {exc}"
        docs = result.stdout.split("---\n")
        for idx, doc in enumerate(docs):
            yaml_doc = yaml.safe_load(doc)
            if not yaml_doc:
                continue
            if yaml_doc.get("kind") == "Namespace":
                break
        else:
            return False, "Convert ContainerLab failed: could not find Namespace component"

        # XXX: we will force namespace to be the configured one, so we remove the namespace generated by clabverter
        del docs[idx]

        return True, "---\n".join(docs)

    def get_topology_visualizer_manifest(self, clab_uuid, topology):
        docs = []
        docs.append(TOPO_VIEW_DEPLOYMENT.replace("%CLAB_UUID%", clab_uuid))
        docs.append(TOPO_VIEW_SERVICE.replace("%CLAB_UUID%", clab_uuid))
        docs.append(
            yaml.dump({
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": f"topology-data-clab-{clab_uuid}"},
                "data": {
                    "topology-data.yaml": yaml.dump(topology),
                },
            })
        )
        return "---\n".join(docs)
