# -*- coding: utf-8 -*-
import os
import subprocess
import yaml
import glob


CLABVERTER_BIN = "/usr/local/bin/clabverter"

if not os.path.exists(CLABVERTER_BIN) or not os.access(CLABVERTER_BIN, os.X_OK):
    raise ValueError("Missing executable 'clabverter'. Please install it from https://github.com/srl-labs/clabernetes/releases/latest")


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

        # Handle absolute paths
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
        # TODO: try to handle topology.kinds and topology.defaults if we have that file in the uploaded_files or one common absolute path

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
            return False, f"Convert ContainerLab failed: {exc} -- {exc.stderr}"
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

