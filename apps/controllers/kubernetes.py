"""Kubernetes Node."""

import time
import os
import sys
import json
import yaml
import string
import re
import traceback
import datetime
import uuid
import random
import subprocess
import base64

from kubernetes import config, client
from kubernetes.stream import stream
from kubernetes.utils import create_from_dict, duration, parse_quantity

from flask import current_app
from flask_login import current_user
from collections import defaultdict

from apps.config import app_config
from apps.utils import format_duration

RNP_TESTBED_NODES = {
    "ids-go": {"lat": -16.67990, "lng": -49.2550},
    "ids-pb": {"lat":  -7.11532, "lng": -34.8610},
    "ids-pe": {"lat":  -8.05428, "lng": -34.8813},
    "ids-rj": {"lat": -22.90350, "lng": -43.2096},
    "ids-rn": {"lat":  -5.79448, "lng": -35.2110},
    "vm1-ac": {"lat":  -9.97400, "lng": -67.8076},
    "vm1-mt": {"lat": -15.59890, "lng": -56.0949},
    "vm1-ro": {"lat":  -8.76183, "lng": -63.9020},
    "whx-ba": {"lat": -12.97040, "lng": -38.5124},
    "whx-pb": {"lat":  -7.23072, "lng": -35.8817},
    "whx-rn": {"lat":  -5.18804, "lng": -37.3441},
}

class K8sController():
    """Kubernetes controller."""

    def __init__(self):
        self.namespace = app_config.K8S_NAMESPACE
        self.v1_api = None
        if not self.namespace:
            return
        try:
            config.load_kube_config(config_file=app_config.K8S_CONFIG)
        except Exception as exc:
            msg = f"Error while loading kube config ({app_config.K8S_CONFIG}): {exc}"
            err = traceback.format_exc().replace("\n", ", ")
            print(msg + " -- " + err)
            return
        self.v1_api = client.CoreV1Api()
        self.apps_v1_api = client.AppsV1Api()
        self.k8s_client = client.ApiClient()
        self.k8s_avoid_nodes = set(app_config.K8S_AVOID_NODES)

        self.identifiers = {
            "pod_hash": self.get_pod_hash,
            "allowed_nodes": self.get_allowed_nodes,
            "allowed_nodes_str": self.get_allowed_nodes_str,
            "choose_one_node": self.choose_one_node,
        }
        self.nodes = {}
        self.ready_nodes = []
        self.nodes_last_updated = 0 

    def try_get_app(self, port_name):
        if not port_name:
            return "http://"
        known_apps = ["https", "http", "ssh", "vnc"]
        for app in known_apps:
            if port_name.startswith(app):
                return app + "://"
        return "http://"

    def list_pods(self):
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        pods = self.v1_api.list_namespaced_pod(
            namespace=self.namespace
        )
        response = []
        for pod in pods.items:
            statuses = [
                status.ready for status in pod.status.container_statuses
            ]
            containers = [
                container.name for container in pod.spec.containers
            ]
            response.append({
                "containers_total": len(statuses),
                "containers_ready": sum(statuses),
                "created": pod.metadata.creation_timestamp,
                "age": format_duration(now - pod.metadata.creation_timestamp),
                "name": pod.metadata.name,
                "uid": pod.metadata.uid,
                "node_name": pod.spec.node_name,
                "pod_ip": pod.status.pod_ip,
                "containers": containers,
                "phase": pod.status.phase,
                "more": yaml.dump(pod.to_dict()),
            })
        return response

    def list_deployments(self):
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        deployments = self.apps_v1_api.list_namespaced_deployment(
            namespace=self.namespace
        )
        response = []
        for dep in deployments.items:
            containers = [
                container.name for container in dep.spec.template.spec.containers
            ]
            response.append({
                "containers_total": dep.status.replicas,
                "containers_ready": dep.status.ready_replicas or 0,
                "created": dep.metadata.creation_timestamp,
                "age": format_duration(now - dep.metadata.creation_timestamp),
                "name": dep.metadata.name,
                "uid": dep.metadata.uid,
                "containers": ",".join(containers),
                "more": yaml.dump(dep.to_dict()),
            })
        return response

    def list_services(self):
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        services = self.v1_api.list_namespaced_service(
            namespace=self.namespace
        )
        response = []
        for srv in services.items:
            ports = [
                f"{port.target_port}:{port.node_port}/{port.protocol}"
                for port in srv.spec.ports
            ]
            response.append({
                "age": format_duration(now - srv.metadata.creation_timestamp),
                "name": srv.metadata.name,
                "uid": srv.metadata.uid,
                "ports": " ".join(ports),
                "type": srv.spec.type,
                "more": yaml.dump(srv.to_dict()),
            })
        return response

    def get_lab_resources(self, resources, published_ports={}):
        labs = []
        owners = set()
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        for resource in resources:
            if isinstance(resource, str):
                owners.add(resource)
            if isinstance(resource, dict) and "uid" in resource:
                owners.add(resource["uid"])

        deployments = self.apps_v1_api.list_namespaced_deployment(
            namespace=self.namespace,
        )
        dep_uid = {}
        for dep in deployments.items:
            try:
                is_child = dep.metadata.owner_references[0].uid in owners
            except:
                is_child = False
            if dep.metadata.uid in owners or is_child:
                dep_uid[dep.metadata.uid] = dep

        owners.update(dep_uid.keys())

        replica_sets = self.apps_v1_api.list_namespaced_replica_set(
            namespace=self.namespace
        )
        rs_uid_to_dep = {}
        for rs in replica_sets.items:
            try:
                dep = dep_uid[rs.metadata.owner_references[0].uid]
            except:
                continue
            rs_uid_to_dep[rs.metadata.uid] = dep

        owners.update(rs_uid_to_dep.keys())

        pod_services = {}
        pod_names = {}
        app_pod_map = defaultdict(list)
        pods = self.v1_api.list_namespaced_pod(
            namespace=self.namespace
        )
        for pod in pods.items:
            pod_labels = pod.metadata.labels or {}
            app = pod_labels.get("app")
            if app:
                app_pod_map[app].append(pod)
            elif clab_name := pod_labels.get("clabernetes/name"):
                app_pod_map[clab_name].append(pod)
            try:
                is_child = pod.metadata.owner_references[0].uid in owners
            except:
                is_child = False
            if pod.metadata.uid not in owners and not is_child:
                continue
            pod_services[pod.metadata.uid] = []
            statuses = [
                status.ready for status in pod.status.container_statuses
            ]
            containers = [
                container.name for container in pod.spec.containers
            ]
            display_name = pod.metadata.name
            if "clabernetes/topologyNode" in pod_labels:
                display_name = pod_labels["clabernetes/topologyNode"]
            pod_names[pod.metadata.uid] = display_name
            labs.append({
                "containers_total": len(statuses),
                "containers_ready": sum(statuses),
                "created": pod.metadata.creation_timestamp,
                "age": duration.format_duration(now - pod.metadata.creation_timestamp.replace(microsecond=0)),
                "name": pod.metadata.name,
                "display_name": display_name,
                "node_name": pod.spec.node_name,
                "pod_ip": pod.status.pod_ip,
                "containers": containers,
                "services": pod_services[pod.metadata.uid],
                "phase": pod.status.phase,
                "more": str(pod),
            })

        services = self.v1_api.list_namespaced_service(
            namespace=self.namespace,
        )
        for srv in services.items:
            srv_labels = srv.metadata.labels or {}
            try:
                is_child = srv.metadata.owner_references[0].uid in owners
            except:
                is_child = False
            if srv.metadata.uid not in owners and not is_child:
                continue
            pods = app_pod_map.get(srv.spec.selector.get("app"), [])
            if not pods and (clab_name := srv.spec.selector.get("clabernetes/name")):
                pods = app_pod_map.get(clab_name, [])

            for port in srv.spec.ports:
                port_name = port.name if port.name else f"{port.port}/{port.protocol}"
                for pod in pods:
                    if str(port.port) not in published_ports.get(pod_names[pod.metadata.uid], []):
                        continue
                    node_ip = self.get_node_ip(pod.spec.node_name)
                    service_link = [
                        port_name,
                        f"{self.try_get_app(port_name)}{node_ip}:{port.node_port}"
                    ]
                    if port.node_port:
                        pod_services[pod.metadata.uid].append(service_link)

        return labs

    def get_labs_by_user(self, f_user_uid, f_lab_id=None):
        if not self.v1_api:
            return []
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        label_selector = "app=hackinsdn-dashboard"
        if f_user_uid:
            label_selector += f",user_uid={f_user_uid}"
        if f_lab_id:
            label_selector += f",lab_id={f_lab_id}"
        labs = defaultdict(list)

        deployments = self.apps_v1_api.list_namespaced_deployment(
            namespace=self.namespace, label_selector=label_selector
        )
        dep_uid = {}
        for dep in deployments.items:
            lab_id = dep.metadata.labels.get("lab_id")
            user_uid = dep.metadata.labels.get("user_uid")
            if not lab_id or not user_uid:
                continue
            dep_uid[dep.metadata.uid] = dep
            containers = [
                container.name for container in dep.spec.template.spec.containers
            ]
            labs[(lab_id, user_uid)].append({
                "kind": "deployment",
                "containers_total": dep.status.replicas,
                "containers_ready": dep.status.ready_replicas,
                "created": dep.metadata.creation_timestamp,
                "age": duration.format_duration(now - dep.metadata.creation_timestamp),
                "name": dep.metadata.name,
                "containers": containers,
                "more": str(dep),
            })

        replica_sets = self.apps_v1_api.list_namespaced_replica_set(
            namespace=self.namespace
        )
        rs_uid_to_dep = {}
        for rs in replica_sets.items:
            try:
                dep = dep_uid[rs.metadata.owner_references[0].uid]
            except:
                continue
            rs_uid_to_dep[rs.metadata.uid] = dep

        pod_services = {}
        app_pod_map = defaultdict(list)
        pods = self.v1_api.list_namespaced_pod(
            namespace=self.namespace
        )
        for pod in pods.items:
            pod_labels = pod.metadata.labels or {}
            app = pod_labels.get("app")
            if app:
                app_pod_map[app].append(pod)
            try:
                dep = rs_uid_to_dep[pod.metadata.owner_references[0].uid]
                dep_labels = dep.metadata.labels or {}
            except:
                dep = None
                dep_labels = {}
            lab_id, user_uid = None, None
            if all([
                app == "hackinsdn-dashboard",
                not f_user_uid or pod_labels.get("user_uid") == f_user_uid,
                not f_lab_id or pod_labels.get("lab_id") == f_lab_id,
            ]):
                lab_id = pod_labels.get("lab_id")
                user_uid = pod_labels.get("user_uid")
            elif dep:
                lab_id = dep_labels.get("lab_id")
                user_uid = dep_labels.get("user_uid")
            if not lab_id or not user_uid:
                continue
            pod_services[pod.metadata.uid] = []
            statuses = [
                status.ready for status in pod.status.container_statuses
            ]
            containers = [
                container.name for container in pod.spec.containers
            ]
            labs[(lab_id, user_uid)].append({
                "kind": "pod",
                "containers_total": len(statuses),
                "containers_ready": sum(statuses),
                "created": pod.metadata.creation_timestamp,
                "age": duration.format_duration(now - pod.metadata.creation_timestamp.replace(microsecond=0)),
                "name": pod.metadata.name,
                "node_name": pod.spec.node_name,
                "pod_ip": pod.status.pod_ip,
                "containers": containers,
                "services": pod_services[pod.metadata.uid],
                "phase": pod.status.phase,
                "more": str(pod),
            })

        services = self.v1_api.list_namespaced_service(
            namespace=self.namespace, label_selector=label_selector
        )
        for srv in services.items:
            srv_labels = srv.metadata.labels or {}
            lab_id = srv_labels.get("lab_id")
            user_uid = srv_labels.get("user_uid")
            if not lab_id or not user_uid:
                continue
            ports = []
            links = []
            node_ips = set()
            for port in srv.spec.ports:
                ports.append(
                    f"{port.target_port}:{port.node_port}/{port.protocol}"
                )
                port_name = port.name if port.name else ports[-1]
                for pod in app_pod_map.get(srv.spec.selector.get("app"), []):
                    node_ip = self.get_node_ip(pod.spec.node_name)
                    node_ips.add(node_ip)
                    service_link = [
                        port_name,
                        f"{self.try_get_app(port_name)}{node_ip}:{port.node_port}"
                    ]
                    if port.node_port:
                        pod_services[pod.metadata.uid].append(service_link)
                    links.append(service_link)
            labs[(lab_id, user_uid)].append({
                "kind": "service",
                "containers_total": 1,
                "containers_ready": 1,
                "created": srv.metadata.creation_timestamp,
                "age": duration.format_duration(now - srv.metadata.creation_timestamp),
                "name": srv.metadata.name,
                "ports": ports,
                "links": links,
                "node_ip": ",".join(node_ips),
                "more": str(srv),
            })

        ## ConfigMap
        config_maps = self.v1_api.list_namespaced_config_map(
            namespace=self.namespace, label_selector=label_selector
        )
        for cfg in config_maps.items:
            cfg_labels = cfg.metadata.labels or {}
            lab_id = cfg_labels.get("lab_id")
            user_uid = cfg_labels.get("user_uid")
            if not lab_id or not user_uid:
                continue
            labs[(lab_id, user_uid)].append({
                "kind": "config_map",
                "containers_total": len(cfg.data),
                "containers_ready": len(cfg.data),
                "created": cfg.metadata.creation_timestamp,
                "age": duration.format_duration(now - cfg.metadata.creation_timestamp),
                "name": cfg.metadata.name,
                "more": str(cfg),
            })
        return labs

    def get_pod_hash(self, **kwargs):
        return kwargs.get("pod_hash", uuid.uuid4().hex[:14])

    def get_allowed_nodes(self, **kwargs):
        if kwargs.get("dry_run"):
            return ["nodeA", "nodeB", "nodeC"]
        if kwargs.get("allowed_nodes"):
            return kwargs["allowed_nodes"]
        self.update_nodes()
        return self.ready_nodes

    def get_allowed_nodes_str(self, **kwargs):
        return ",".join(self.get_allowed_nodes(**kwargs))

    def choose_one_node(self, **kwargs):
        if kwargs.get("dry_run"):
            return "nodeA"
        if kwargs.get("allowed_nodes"):
            return random.choice(kwargs["allowed_nodes"])
        self.update_nodes()
        return random.choice(self.ready_nodes)

    def update_nodes(self):
        if time.time() - self.nodes_last_updated < 60:
            return
        self.nodes = {}
        self.ready_nodes = []
        resp = self.v1_api.list_node()
        for node in resp.items:
            self.nodes[node.metadata.name] = node
            # update ready_nodes unless we should avoid this node
            if node.metadata.name in self.k8s_avoid_nodes:
                continue
            for cond in node.status.conditions:
                if cond.type == "Ready" and cond.status == "True":
                    self.ready_nodes.append(node.metadata.name)
        self.nodes_last_updated = time.time()

    def get_node_ip(self, name):
        self.update_nodes()
        node = self.nodes.get(name)
        if not node:
            return None
        for addr in node.status.addresses:
            if addr.type == "InternalIP":
                return addr.address
        return None

    def get_identifier_func(self, identf):
        if identf in self.identifiers:
            return self.identifiers[identf]
        for key in self.identifiers:
            r = re.compile(f"^{key}$")
            if r.match(identf):
                return self.identifiers[key]
        return None

    def substitute_identifiers(
        self,
        manifest,
        user_uid=None,
        pod_hash=None,
        allowed_nodes=None,
        dry_run=False,
    ):
        try:
            tmpl = string.Template(manifest)
        except Exception as exc:
            current_app.logger.warning(f"Invalid manifest content {exc}")
            return False, f"Failed to read manifest content: {exc}"
        mapping = {}
        for identf in tmpl.get_identifiers():
            identf_func = self.get_identifier_func(identf)
            if not identf_func:
                return False, f"Invalid placeholder {identf}"
            try:
                mapping[identf] = identf_func(
                    dry_run=dry_run,
                    user_uid=user_uid,
                    pod_hash=pod_hash,
                    allowed_nodes=allowed_nodes,
                )
            except Exception as exc:
                msg = f"Error while processing template: {exc}"
                err = traceback.format_exc().replace("\n", ", ")
                current_app.logger.error(msg + " -- " + err)
                return False, msg
        try:
            data = tmpl.safe_substitute(mapping)
        except Exception as exc:
            # app.logger.info(f"Invalid manifest {filename} {exc}")
            return False, f"Failed to apply placeholders: {exc}"

        return True, data

    def get_k8s_resource(self, resource):
        """Get k8s resource using kubectl."""
        try:
            result = subprocess.run(
                ["kubectl", "get", resource["kind"], resource["name"], "-o", "json"],
                capture_output=True,
                text=True,
                check=True
            )
            result = json.loads(result.stdout)
        except subprocess.CalledProcessError as exc:
            raise Exception(f"Failed to get k8s resource: {exc} -- {exc.stderr}")
        except Exception as exc:
            raise Exception(f"Failed to get k8s resource: {exc}")
        result["is_ok"] = True
        return result

    def create_k8s_resource(self, resource):
        """Create k8s resource trying to use kubernetes lib and fallback to kubectl."""
        if resource["kind"] in ["Pod", "Service", "Deployment", "ConfigMap"]:
            k8s_objs = create_from_dict(
                self.k8s_client,
                data=resource,
                namespace=self.namespace,
            )
            return k8s_objs[0].to_dict()
        try:
            result = subprocess.run(
                ["kubectl", "create", "-f", "-", "-o", "json"],
                input=json.dumps(resource),
                capture_output=True,
                text=True,
                check=True
            )
            result = json.loads(result.stdout)
        except subprocess.CalledProcessError as exc:
            raise Exception(f"Failed to create k8s resource: {exc} -- {exc.stderr}")
        except Exception as exc:
            raise Exception(f"Failed to create k8s resource: {exc}")
        return result

    def delete_k8s_resource(self, resource):
        """Delete k8s resource using kubectl."""
        try:
            result = subprocess.run(
                ["kubectl", "delete", resource["kind"], resource["name"]],
                capture_output=True,
                text=True,
                check=True
            )
        except subprocess.CalledProcessError as exc:
            current_app.logger.error(f"Failed to delete k8s resource: {exc} -- {exc.stderr}")
            return False
        except Exception as exc:
            current_app.logger.error(f"Failed to delete k8s resource: {exc}")
            return False
        return True

    def create_lab(
        self,
        lab_id,
        manifest,
        dry_run=False,
        replace_identifiers=True,
        user_uid=None,
        pod_hash=None,
        allowed_nodes=None,
    ):
        """create lab according to manifest and labels"""
        data = manifest
        if replace_identifiers:
            status, result = self.substitute_identifiers(
                manifest,
                user_uid=user_uid,
                pod_hash=pod_hash,
                dry_run=dry_run,
                allowed_nodes=allowed_nodes,
            )
            if not status:
                return status, result
            data = result

        yaml_docs = []
        try:
            for doc in yaml.load_all(data, Loader=yaml.Loader):
                doc.setdefault("metadata", {})
                doc["metadata"].setdefault("labels", {})
                doc["metadata"]["labels"] = {
                    "app": "hackinsdn-dashboard",
                    "user_uid": user_uid,
                    "lab_id": lab_id,
                }
                yaml_docs.append(doc)
        except Exception as exc:
            msg = f"Failed to load manifest yaml: {exc}"
            err = traceback.format_exc().replace("\n", ", ")
            current_app.logger.error(msg + " -- " + err)
            return False, msg

        if dry_run:
            return True, "OK"

        results = []
        msg_fail = None
        for doc in yaml_docs:
            if doc is None:
                continue
            try:
                result = self.create_k8s_resource(doc)
            except Exception as exc:
                err = traceback.format_exc().replace("\n", ", ")
                msg_fail = f"Failed to create resources on Kubernentes: {exc}"
                current_app.logger.error(f"{msg_fail} {err=} {doc=}")
                break
            results.append({
                "kind": result["kind"],
                "name": result["metadata"]["name"],
                "uid": result["metadata"]["uid"],
            })

        if msg_fail:
            current_app.logger.error(f"Rollback resource creation due to failures! To be removed: {results}")
            self.delete_resources_by_name(results)
            self._wait_gone(results, timeout=10)
            return False, msg_fail

        return True, results

    def create_registry_secret(self, name, server, username, password):
        """Create to pull an image from a private container image registry or repository."""
        auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
        docker_config_dict = {
            "auths": {
                server: {
                    "username": username,
                    "password": password,
                    "auth": auth,
                }
            }
        }
        docker_config = base64.b64encode(
            json.dumps(docker_config_dict).encode("utf-8")
        ).decode("utf-8")
        try:
            self.v1_api.create_namespaced_secret(
                namespace=self.namespace,
                body=client.V1Secret(
                    metadata=client.V1ObjectMeta(
                        name=name,
                    ),
                    type="kubernetes.io/dockerconfigjson",
                    data={".dockerconfigjson": docker_config},
                ),
            )
        except Exception as exc:
            msg = f"Failed to create secret: {exc}"
            err = traceback.format_exc().replace("\n", ", ")
            current_app.logger.error(msg + " -- " + err)
            return False, msg
        return True, "secret cretated"

    def validate_token(self, token):
        """Check if this token is authorized to access the API."""
        return True

    def get_pods_by_lab_id(self, lab_id):
        """Return all pods with a certain lab_id label."""
        return []

    def get_pod_by_name(self, pod):
        """Return pod by its name."""
        pod = self.v1_api.read_namespaced_pod(
            name=pod["name"], namespace=self.namespace
        )
        pod_dict = pod.to_dict()
        pod_dict["is_ok"] = pod.status.phase == "Running"
        return pod_dict

    def get_deployment_by_name(self, deployment):
        """Return deployment by its name."""
        deployment = self.apps_v1_api.read_namespaced_deployment(
            name=deployment["name"], namespace=self.namespace
        )
        dep_dict = deployment.to_dict()
        dep_dict["is_ok"] = deployment.status.replicas == deployment.status.ready_replicas
        return dep_dict

    def get_service_by_name(self, service):
        """Return service by its name."""
        service = self.v1_api.read_namespaced_service(
            name=service["name"], namespace=self.namespace
        )
        service_dict = service.to_dict()
        service_dict["is_ok"] = True
        return service_dict

    def get_config_map_by_name(self, config_map):
        """Return config_map by its name."""
        config_map = self.v1_api.read_namespaced_config_map(
            name=config_map["name"], namespace=self.namespace
        )
        config_map_dict = config_map.to_dict()
        config_map_dict["is_ok"] = True
        return config_map_dict

    def get_resources_by_name(self, resources):
        """Return resources by their name and kind."""
        results = []
        for resource in resources:
            if resource["kind"] == "Pod":
                results.append(self.get_pod_by_name(resource))
            elif resource["kind"] == "Service":
                results.append(self.get_service_by_name(resource))
            elif resource["kind"] == "Deployment":
                results.append(self.get_deployment_by_name(resource))
            elif resource["kind"] == "ConfigMap":
                results.append(self.get_config_map_by_name(resource))
            else:
                results.append(self.get_k8s_resource(resource))
        return results

    def delete_pod_by_name(self, pod):
        """Delete pod by its name."""
        try:
            self.v1_api.delete_namespaced_pod(
                name=pod["name"], namespace=self.namespace
            )
        except Exception as exc:
            current_app.logger.warning(f"Failed to delete pod {pod['name']} {exc}")
            return False
        return True

    def delete_deployment_by_name(self, deployment):
        """Delete deployment by its name."""
        try:
            self.apps_v1_api.delete_namespaced_deployment(
                name=deployment["name"], namespace=self.namespace
            )
        except Exception as exc:
            current_app.logger.warning(f"Failed to delete deployment {deployment['name']} {exc}")
            return False
        return True

    def delete_service_by_name(self, service):
        """Delete service by its name."""
        try:
            self.v1_api.delete_namespaced_service(
                name=service["name"], namespace=self.namespace
            )
        except Exception as exc:
            current_app.logger.warning(f"Failed to delete service {service['name']} {exc}")
            return False
        return True

    def delete_config_map_by_name(self, config_map):
        """Delete config_map by its name."""
        try:
            self.v1_api.delete_namespaced_config_map(
                name=config_map["name"], namespace=self.namespace
            )
        except Exception as exc:
            current_app.logger.warning(f"Failed to delete configmap {config_map['name']} {exc}")
            return False
        return True

    def delete_secret_by_name(self, secret):
        """Delete secret by its name."""
        name = secret["name"] if isinstance(secret, dict) else secret
        try:
            self.v1_api.delete_namespaced_secret(
                name=name, namespace=self.namespace
            )
        except Exception as exc:
            current_app.logger.warning(f"Failed to delete secret {name}: {exc}")
            return False
        return True

    def delete_resources_by_name(self, resources):
        """Delete resources by their name and kind."""
        results = []
        for resource in resources:
            if resource["kind"] == "Pod":
                results.append(self.delete_pod_by_name(resource))
            elif resource["kind"] == "Service":
                results.append(self.delete_service_by_name(resource))
            elif resource["kind"] == "Deployment":
                results.append(self.delete_deployment_by_name(resource))
            elif resource["kind"] == "ConfigMap":
                results.append(self.delete_config_map_by_name(resource))
            elif resource["kind"] == "Secret":
                results.append(self.delete_secret_by_name(resource))
            else:
                results.append(self.delete_k8s_resource(resource))
        return results

    def _wait_gone(self, resources, timeout=10):
        """Wait for resources to be removed from the APIServer, or timeout to be exceeded."""
        start = time.time()
        while time.time() - start < timeout:
            has_pending = False
            for resource in resources:
                try:
                    self.get_resources_by_name([resource])
                except Exception:
                    continue
                has_pending = True
            if not has_pending:
                return True
            time.sleep(0.5)
        return False

    def get_nodes(self):
        result = []
        self.update_nodes()
        for name, node in self.nodes.items():
            result.append({
                "name": name,
                "status": "Ready" if name in self.ready_nodes else "NotReady",
                "latitude": RNP_TESTBED_NODES.get(name, {}).get("lat", 0.0),
                "longitude": RNP_TESTBED_NODES.get(name, {}).get("lng", 0.0),
            })
        return result

    def humanbytes(self, decimal):
        """Return the given bytes as a human friendly KB, MB, GB, or TB string."""
        B = float(decimal)
        KB = float(1024)
        MB = float(KB ** 2)
        GB = float(KB ** 3)
        TB = float(KB ** 4)
        PB = float(KB ** 5)
        EB = float(KB ** 6)

        if B < KB:
            return '{0} {1}'.format(B,'Bytes' if 0 == B > 1 else 'Byte')
        elif KB <= B < MB:
            return '{0:.2f} KB'.format(B / KB)
        elif MB <= B < GB:
            return '{0:.2f} MB'.format(B / MB)
        elif GB <= B < TB:
            return '{0:.2f} GB'.format(B / GB)
        elif TB <= B < PB:
            return '{0:.2f} TB'.format(B / TB)
        elif PB <= B < EB:
            return '{0:.2f} PB'.format(B / PB)
        elif EB <= B:
            return '{0:.2f} EB'.format(B / EB)

    def get_statistics(self):
        """Get statistics of the cluster."""
        self.update_nodes()
        total_cpu_capacity = 0
        total_memory_capacity = 0
        total_storage_capacity = 0
        total_pods = 0
        total_nodes = len(self.ready_nodes)

        for node_name in self.ready_nodes:
            node = self.nodes.get(node_name)
            if not node:
                continue

            # CPU
            cpu_capacity = int(node.status.capacity.get("cpu", 0))
            total_cpu_capacity += cpu_capacity

            # Memory
            try:
                memory_capacity = parse_quantity(node.status.capacity.get("memory", "0"))
            except:
                memory_capacity = 0
            total_memory_capacity += memory_capacity

            # Storage
            try:
                storage_capacity = parse_quantity(node.status.capacity.get("ephemeral-storage", "0"))
            except:
                storage_capacity = 0
            total_storage_capacity += storage_capacity

            # Pods
            pods_capacity = int(node.status.capacity.get("pods", 0))
            total_pods += pods_capacity

        return {
            "total_cpu_capacity": total_cpu_capacity,
            "total_memory_capacity": self.humanbytes(total_memory_capacity),
            "total_storage_capacity": self.humanbytes(total_storage_capacity),
            "total_pods": total_pods,
            "total_nodes": total_nodes,
        }
