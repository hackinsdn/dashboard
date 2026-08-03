"""Kubernetes Node."""

import time
import os
import sys
import json
import yaml
import string
import re
import socket
import threading
import functools
import traceback
import datetime
import uuid
import random
import subprocess
import base64

import urllib3

from kubernetes import config, client
from kubernetes.stream import stream
from kubernetes.utils import create_from_dict, duration, parse_quantity
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from flask import current_app
from flask_login import current_user
from collections import defaultdict, deque

from apps.config import app_config
from apps.utils import format_duration


# kubectl stderr fragments that indicate the API server is unreachable or the
# credentials in the current kubeconfig were rejected -> worth failing over to
# the next kubeconfig. Anything else (NotFound, AlreadyExists, validation, ...)
# means the cluster answered a valid request and must NOT trigger a failover.
_KUBECTL_FAILOVER_STRINGS = (
    "unable to connect to the server",
    "connection refused",
    "i/o timeout",
    "tls handshake timeout",
    "no route to host",
    "eof",
    "unauthorized",
    "you must be logged in",
    "the server has asked for the client to provide credentials",
    "forbidden",
)

# ApiException HTTP statuses that mean "try another kubeconfig": auth failures
# (an HA path may carry an expired/invalid credential) and server-side / no
# response conditions.
_AUTH_STATUSES = (401, 403)


class _FailoverError(Exception):
    """A connectivity/auth failure a wrapper raises to trigger failover while
    still carrying a human-friendly message (so the message survives when all
    kubeconfigs are exhausted and the error propagates to the caller)."""

    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)


def _kubectl_stderr_is_failover(stderr):
    """True when a kubectl stderr indicates a connectivity/auth failure."""
    if not stderr:
        return False
    low = stderr.lower()
    return any(s in low for s in _KUBECTL_FAILOVER_STRINGS)


def _is_failover_error(exc):
    """Classify an exception as a failover trigger (connectivity or auth).

    Connectivity: timeouts, connection errors, API status 0/None, HTTP 5xx, and
    kubeconfig build/load errors (a broken standby file). Auth: HTTP 401/403.
    Every other ApiException (a valid request the cluster answered: 400/404/409/
    422/...) returns False so it propagates to the caller unchanged.
    """
    if isinstance(exc, _FailoverError):
        return True
    if isinstance(exc, ApiException):
        status = exc.status or 0
        return status == 0 or status in _AUTH_STATUSES or status >= 500
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    if isinstance(exc, subprocess.CalledProcessError):
        return _kubectl_stderr_is_failover(exc.stderr)
    if isinstance(exc, (ConfigException, OSError)):
        # broken/absent kubeconfig for this endpoint -> move to the next one
        return True
    return isinstance(exc, (
        urllib3.exceptions.MaxRetryError,
        urllib3.exceptions.ReadTimeoutError,
        urllib3.exceptions.ConnectTimeoutError,
        urllib3.exceptions.ProtocolError,
        socket.timeout,
        TimeoutError,
        ConnectionError,
    ))


def _reason_for(exc):
    """A short, log/metric-friendly tag for why a failover happened."""
    if isinstance(exc, _FailoverError):
        return exc.reason
    if isinstance(exc, ApiException):
        status = exc.status or 0
        if status == 401:
            return "auth_401"
        if status == 403:
            return "auth_403"
        if status >= 500:
            return "server_5xx"
        return "conn_error"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    if isinstance(exc, subprocess.CalledProcessError):
        low = (exc.stderr or "").lower()
        if "unauthorized" in low or "you must be logged in" in low or "credentials" in low:
            return "auth"
        if "forbidden" in low:
            return "auth"
        return "conn_refused"
    if isinstance(exc, (ConfigException, OSError)):
        return "config_error"
    if isinstance(exc, (urllib3.exceptions.ReadTimeoutError,
                        urllib3.exceptions.ConnectTimeoutError,
                        socket.timeout, TimeoutError)):
        return "timeout"
    return "conn_refused"


_RAISE = object()  # sentinel: re-raise the last error when all kubeconfigs fail


def with_failover(method=None, *, default=_RAISE):
    """Retry a controller method against the next kubeconfig on a failover error.

    Sticky + circular: the active endpoint is advanced only when the call fails
    for a connectivity/auth reason, and each endpoint is tried at most once per
    call. When every kubeconfig has failed, the last error is re-raised -- unless
    ``default`` is given, in which case it is returned instead (used by the
    best-effort delete/secret helpers that historically returned False rather
    than raising).
    """
    def decorate(method):
        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            if not self._endpoints:
                # disabled controller: behave as the single-config code path
                return method(self, *args, **kwargs)
            last_exc = None
            for _ in range(len(self._endpoints)):
                ep = self._current_endpoint()
                try:
                    return method(self, *args, **kwargs)
                except Exception as exc:
                    if not _is_failover_error(exc):
                        raise
                    last_exc = exc
                    self._record_failover(ep, exc)
                    self._rotate_from(ep)
            if default is not _RAISE:
                current_app.logger.error(
                    f"k8s: all kubeconfigs failed for {method.__name__}: {last_exc}"
                )
                return default
            raise last_exc
        return wrapper

    # support both @with_failover and @with_failover(default=...)
    return decorate(method) if callable(method) else decorate


class _KubeEndpoint:
    """One kubeconfig file and its (lazily built) API clients.

    Each endpoint owns an isolated ``client.Configuration`` so switching the
    active endpoint never mutates state shared with another one.
    """

    def __init__(self, path):
        self.path = path
        self.built = False
        self.v1_api = None
        self.apps_v1_api = None
        self.discovery_api = None
        self.k8s_client = None

    def ensure_built(self, lock):
        if self.built:
            return
        with lock:
            if self.built:
                return
            cfg = client.Configuration()
            config.load_kube_config(config_file=self.path, client_configuration=cfg)
            # do not retry a failed call: urllib3 retries connection errors ~3
            # times by default, multiplying the wall-clock time an unreachable
            # API server can block a request (the request timeout is per-attempt).
            # Failover to the next kubeconfig is our retry mechanism instead.
            cfg.retries = 0
            api = client.ApiClient(configuration=cfg)
            self.v1_api = client.CoreV1Api(api)
            self.apps_v1_api = client.AppsV1Api(api)
            self.discovery_api = client.DiscoveryV1Api(api)
            self.k8s_client = api
            self.built = True


class K8sController():
    """Kubernetes controller."""

    def __init__(self):
        self.namespace = app_config.K8S_NAMESPACE
        # failover state must exist even when the controller is disabled, since
        # the client properties read self._endpoints.
        self._lock = threading.Lock()
        self._endpoints = []
        self._active_idx = 0
        self._flap_logged = False
        self._failover_stats = None
        if not self.namespace:
            return
        # timeout (seconds) for every Kubernetes API call, so the app does not
        # hang indefinitely when the API server is unreachable. Scalar form is
        # used by the kubectl subprocess calls; the (connect, read) tuple is used
        # by the kubernetes client, whose single-value form only honors an int
        # (a float is silently ignored, leaving the call without any timeout).
        self.request_timeout = app_config.K8S_REQUEST_TIMEOUT
        self.api_request_timeout = (self.request_timeout, self.request_timeout)
        self.flap_threshold = app_config.K8S_FLAP_THRESHOLD
        self._endpoints = [_KubeEndpoint(p) for p in app_config.K8S_CONFIGS]
        self._init_failover_stats()
        self.k8s_avoid_nodes = set(app_config.K8S_AVOID_NODES)
        self.k8s_nodes_geotag = app_config.TESTBED_NODES_GEOTAG

        self.identifiers = {
            "pod_hash": self.get_pod_hash,
            "allowed_nodes": self.get_allowed_nodes,
            "allowed_nodes_str": self.get_allowed_nodes_str,
            "choose_one_node": self.choose_one_node,
        }
        self.nodes = {}
        self.ready_nodes = []
        self.nodes_last_updated = 0

        # eagerly build the preferred endpoint so a misconfiguration surfaces at
        # startup (mirrors the old behaviour); standbys stay lazy. A failure
        # here is non-fatal: the failover machinery will try it (and the others)
        # again on the first real call.
        try:
            self._current_endpoint().ensure_built(self._lock)
        except Exception as exc:
            msg = f"Error while loading kube config ({app_config.K8S_CONFIG}): {exc}"
            err = traceback.format_exc().replace("\n", ", ")
            print(msg + " -- " + err)

    # -- multi-kubeconfig plumbing ----------------------------------------

    def _current_endpoint(self):
        """Active endpoint (not built); None when the controller is disabled."""
        if not self._endpoints:
            return None
        return self._endpoints[self._active_idx]

    def _client(self, attr):
        """Return the active endpoint's client, building it lazily."""
        ep = self._current_endpoint()
        if ep is None:
            return None
        ep.ensure_built(self._lock)
        return getattr(ep, attr)

    @property
    def v1_api(self):
        return self._client("v1_api")

    @property
    def apps_v1_api(self):
        return self._client("apps_v1_api")

    @property
    def discovery_api(self):
        return self._client("discovery_api")

    @property
    def k8s_client(self):
        return self._client("k8s_client")

    def _kubectl_base(self):
        """kubectl command prefix pinned to the active kubeconfig."""
        ep = self._current_endpoint()
        if ep is None:
            return ["kubectl"]
        return ["kubectl", "--kubeconfig", ep.path]

    def _init_failover_stats(self):
        active = self._endpoints[0].path if self._endpoints else None
        self._failover_stats = {
            "active_path": active,
            "active_since": time.time(),
            "total_failovers": 0,
            "per_endpoint": {
                ep.path: {"failovers": 0, "last_error": None, "last_failover_ts": 0.0}
                for ep in self._endpoints
            },
            "recent": deque(maxlen=50),
        }

    def _rotate_from(self, failed_ep):
        """Advance the active endpoint, but only if it is still the failed one.

        The compare-and-swap keeps a burst of concurrent failures (gevent
        greenlets all hitting the same dead endpoint) from skipping past healthy
        endpoints: the fleet advances exactly one step.
        """
        with self._lock:
            if not self._endpoints or self._endpoints[self._active_idx] is not failed_ep:
                return
            self._active_idx = (self._active_idx + 1) % len(self._endpoints)
            new_ep = self._endpoints[self._active_idx]
            self._failover_stats["active_path"] = new_ep.path
            self._failover_stats["active_since"] = time.time()
        current_app.logger.info(f"k8s active kubeconfig is now {new_ep.path}")

    def _record_failover(self, failed_ep, exc):
        """Count a failover, log it, and edge-trigger the flapping alarm."""
        reason = _reason_for(exc)
        now = time.time()
        n_endpoints = len(self._endpoints)
        next_path = failed_ep.path
        if n_endpoints > 1:
            nxt_idx = (self._endpoints.index(failed_ep) + 1) % n_endpoints
            next_path = self._endpoints[nxt_idx].path
        with self._lock:
            st = self._failover_stats
            st["total_failovers"] += 1
            pe = st["per_endpoint"].setdefault(
                failed_ep.path,
                {"failovers": 0, "last_error": None, "last_failover_ts": 0.0},
            )
            pe["failovers"] += 1
            pe["last_error"] = f"{type(exc).__name__}: {reason}"
            pe["last_failover_ts"] = now
            st["recent"].append({
                "ts": now, "from": failed_ep.path, "to": next_path, "reason": reason,
            })
            recent_5m = sum(1 for e in st["recent"] if now - e["ts"] <= 300)
        current_app.logger.warning(
            f"k8s failover: {failed_ep.path} -> {next_path} reason={reason} ({exc})"
        )
        # edge-triggered so a flapping set is logged once, not on every rotation
        if recent_5m >= self.flap_threshold and not self._flap_logged:
            self._flap_logged = True
            current_app.logger.error(
                f"k8s endpoints flapping: {recent_5m} failovers in last 5m"
            )
        elif recent_5m < self.flap_threshold:
            self._flap_logged = False

    def get_failover_stats(self):
        """Failover metrics for get_statistics()/health; None when <2 configs."""
        if len(self._endpoints) < 2:
            return None
        now = time.time()
        with self._lock:
            st = self._failover_stats
            recent_5m = sum(1 for e in st["recent"] if now - e["ts"] <= 300)
            active_path = st["active_path"]
            return {
                "active": active_path,
                "active_since": st["active_since"],
                "total_failovers": st["total_failovers"],
                "failovers_last_5m": recent_5m,
                "is_flapping": recent_5m >= self.flap_threshold,
                "endpoints": [
                    {
                        "path": ep.path,
                        "failovers": st["per_endpoint"].get(ep.path, {}).get("failovers", 0),
                        "last_error": st["per_endpoint"].get(ep.path, {}).get("last_error"),
                        "active": ep.path == active_path,
                    }
                    for ep in self._endpoints
                ],
            }

    def try_get_app(self, port_name):
        if not port_name:
            return "http://"
        known_apps = ["https", "http", "ssh", "vnc"]
        for app in known_apps:
            if port_name.startswith(app):
                return app + "://"
        return "http://"

    @with_failover
    def list_pods(self):
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        pods = self.v1_api.list_namespaced_pod(
            namespace=self.namespace, _request_timeout=self.api_request_timeout
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

    @with_failover
    def list_deployments(self):
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        deployments = self.apps_v1_api.list_namespaced_deployment(
            namespace=self.namespace, _request_timeout=self.api_request_timeout
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

    @with_failover
    def list_services(self):
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        services = self.v1_api.list_namespaced_service(
            namespace=self.namespace, _request_timeout=self.api_request_timeout
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

    @with_failover
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
            namespace=self.namespace, _request_timeout=self.api_request_timeout,
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
            namespace=self.namespace, _request_timeout=self.api_request_timeout
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
        pods_by_uid = {}
        pods = self.v1_api.list_namespaced_pod(
            namespace=self.namespace, _request_timeout=self.api_request_timeout
        )
        for pod in pods.items:
            pods_by_uid[pod.metadata.uid] = pod
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
            if "hackinsdn/displayName" in pod_labels:
                display_name = pod_labels["hackinsdn/displayName"]
            elif "clabernetes/topologyNode" in pod_labels:
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
                "labels": pod_labels,
                "more": str(pod),
            })

        service_to_pods = defaultdict(list)
        endpoint_slices = self.discovery_api.list_namespaced_endpoint_slice(
            namespace=self.namespace, _request_timeout=self.api_request_timeout
        )
        for slice_item in endpoint_slices.items:
            slice_pods = []
            for ep in slice_item.endpoints or []:
                # targetRef is optional (e.g. endpoints backing external IPs)
                if ep.target_ref and ep.target_ref.uid in pods_by_uid:
                    slice_pods.append(pods_by_uid[ep.target_ref.uid])
            # ownerReferences is optional (manually managed slices)
            for own_ref in slice_item.metadata.owner_references or []:
                service_to_pods[own_ref.uid].extend(slice_pods)

        services = self.v1_api.list_namespaced_service(
            namespace=self.namespace, _request_timeout=self.api_request_timeout,
        )
        for srv in services.items:
            srv_labels = srv.metadata.labels or {}
            try:
                is_child = srv.metadata.owner_references[0].uid in owners
            except:
                is_child = False
            if srv.metadata.uid not in owners and not is_child:
                continue
            # selector is optional: selectorless services are resolved via
            # their endpoint slices below
            selector = srv.spec.selector or {}
            pods = app_pod_map.get(selector.get("app"), [])
            if not pods and (clab_name := selector.get("clabernetes/name")):
                pods = app_pod_map.get(clab_name, [])
            if not pods:
                # only pods that belong to this lab (they are the ones with a
                # pod_services entry) - a slice may reference foreign pods
                pods = [
                    pod for pod in service_to_pods.get(srv.metadata.uid, [])
                    if pod.metadata.uid in pod_services
                ]

            for port in srv.spec.ports:
                port_name = port.name if port.name else f"{port.port}/{port.protocol}"
                for pod in pods:
                    if "clabernetes/topologyServiceType" in srv_labels and str(port.port) not in published_ports.get(pod_names[pod.metadata.uid], []):
                        continue
                    node_ip = self.get_node_ip(pod.spec.node_name)
                    service_link = [
                        port_name,
                        f"{self.try_get_app(port_name)}{node_ip}:{port.node_port}"
                    ]
                    if port.node_port:
                        pod_services[pod.metadata.uid].append(service_link)

        return labs

    @with_failover
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
            namespace=self.namespace, label_selector=label_selector,
            _request_timeout=self.api_request_timeout,
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
            namespace=self.namespace, _request_timeout=self.api_request_timeout
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
            namespace=self.namespace, _request_timeout=self.api_request_timeout
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
            namespace=self.namespace, label_selector=label_selector,
            _request_timeout=self.api_request_timeout,
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
            namespace=self.namespace, label_selector=label_selector,
            _request_timeout=self.api_request_timeout,
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

    @with_failover
    def update_nodes(self):
        if time.time() - self.nodes_last_updated < 60:
            return
        self.nodes = {}
        self.ready_nodes = []
        resp = self.v1_api.list_node(_request_timeout=self.api_request_timeout)
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
                continue
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

    @with_failover
    def get_k8s_resource(self, resource):
        """Get k8s resource using kubectl."""
        try:
            result = subprocess.run(
                self._kubectl_base() + ["get", resource["kind"], resource["name"], "-o", "json"],
                capture_output=True,
                text=True,
                check=True,
                timeout=self.request_timeout,
            )
            result = json.loads(result.stdout)
        except subprocess.TimeoutExpired as exc:
            raise _FailoverError("timeout", f"Timeout while getting k8s resource: {exc}")
        except subprocess.CalledProcessError as exc:
            if _kubectl_stderr_is_failover(exc.stderr):
                raise _FailoverError(_reason_for(exc), f"Failed to get k8s resource: {exc} -- {exc.stderr}")
            raise Exception(f"Failed to get k8s resource: {exc} -- {exc.stderr}")
        except Exception as exc:
            raise Exception(f"Failed to get k8s resource: {exc}")
        if resource["kind"] == "Topology":
            result["is_ok"] = result.get("status", {}).get("topologyReady", False)
        else:
            result["is_ok"] = True
        return result

    @with_failover
    def create_k8s_resource(self, resource):
        """Create k8s resource trying to use kubernetes lib and fallback to kubectl.

        Failover granularity is deliberately this single-resource call (not
        create_lab): if the active endpoint dies mid-manifest, only the failing
        doc is retried on the sibling endpoint (same cluster) so the lab still
        completes; a genuine 409/AlreadyExists is not a failover error and flows
        into create_lab's rollback.
        """
        if resource["kind"] in ["Pod", "Service", "Deployment", "ConfigMap"]:
            k8s_objs = create_from_dict(
                self.k8s_client,
                data=resource,
                namespace=self.namespace,
                _request_timeout=self.api_request_timeout,
            )
            return k8s_objs[0].to_dict()
        try:
            result = subprocess.run(
                self._kubectl_base() + ["create", "-f", "-", "-o", "json"],
                input=json.dumps(resource),
                capture_output=True,
                text=True,
                check=True,
                timeout=self.request_timeout,
            )
            result = json.loads(result.stdout)
        except subprocess.TimeoutExpired as exc:
            raise _FailoverError("timeout", f"Timeout while creating k8s resource: {exc}")
        except subprocess.CalledProcessError as exc:
            if _kubectl_stderr_is_failover(exc.stderr):
                raise _FailoverError(_reason_for(exc), f"Failed to create k8s resource: {exc} -- {exc.stderr}")
            raise Exception(f"Failed to create k8s resource: {exc} -- {exc.stderr}")
        except Exception as exc:
            raise Exception(f"Failed to create k8s resource: {exc}")
        return result

    @with_failover(default=False)
    def delete_k8s_resource(self, resource):
        """Delete k8s resource using kubectl."""
        try:
            result = subprocess.run(
                self._kubectl_base() + ["delete", resource["kind"], resource["name"], "--timeout=10s"],
                capture_output=True,
                text=True,
                check=True,
                timeout=self.request_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise _FailoverError("timeout", f"Timeout while deleting k8s resource: {exc}")
        except subprocess.CalledProcessError as exc:
            if _kubectl_stderr_is_failover(exc.stderr):
                raise _FailoverError(_reason_for(exc), f"Failed to delete k8s resource: {exc} -- {exc.stderr}")
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

    @with_failover(default=(False, "Failed to create secret: all kubeconfigs unreachable"))
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
                _request_timeout=self.api_request_timeout,
            )
        except Exception as exc:
            if _is_failover_error(exc):
                raise  # let @with_failover try the next kubeconfig
            msg = f"Failed to create secret: {exc}"
            err = traceback.format_exc().replace("\n", ", ")
            current_app.logger.error(msg + " -- " + err)
            return False, msg
        return True, "secret cretated"

    @with_failover(default=(False, "Failed to sync lab data: all kubeconfigs unreachable"))
    def sync_labdata_configmaps(self, lab_id, entries, files_dir):
        """Reconcile the per-file lab-data ConfigMaps of a lab.

        Each entry becomes its own ConfigMap (entry["configmap_name"]) holding
        the single file keyed by its original filename; text is stored in
        ``data`` and binary in ``binaryData`` (base64). Every ConfigMap carries
        ``lab_id`` + ``hackinsdn.io/labdata`` labels so files removed since the
        previous save can be located by selector and pruned.
        """
        desired = {}
        for entry in entries:
            cm_name = entry["configmap_name"]
            key = entry["original_name"]
            fpath = os.path.join(files_dir, entry["filename"])
            try:
                with open(fpath, "rb") as fh:
                    raw = fh.read()
            except FileNotFoundError:
                current_app.logger.warning(
                    f"Lab data file missing on disk, skipping ConfigMap {cm_name}: {fpath}"
                )
                continue
            body = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name=cm_name,
                    labels={
                        "app": "hackinsdn-dashboard",
                        "lab_id": lab_id,
                        "hackinsdn.io/labdata": "true",
                    },
                ),
            )
            try:
                body.data = {key: raw.decode("utf-8")}
            except UnicodeDecodeError:
                body.binary_data = {key: base64.b64encode(raw).decode("ascii")}
            desired[cm_name] = body

        # create or replace the ConfigMaps for the files currently attached
        for cm_name, body in desired.items():
            try:
                self.v1_api.read_namespaced_config_map(
                    name=cm_name, namespace=self.namespace,
                    _request_timeout=self.api_request_timeout,
                )
                exists = True
            except ApiException as exc:
                if exc.status == 404:
                    exists = False
                else:
                    if _is_failover_error(exc):
                        raise
                    msg = f"Failed to read ConfigMap {cm_name}: {exc}"
                    current_app.logger.error(msg)
                    return False, msg
            try:
                if exists:
                    self.v1_api.replace_namespaced_config_map(
                        name=cm_name, namespace=self.namespace, body=body,
                        _request_timeout=self.api_request_timeout,
                    )
                else:
                    self.v1_api.create_namespaced_config_map(
                        namespace=self.namespace, body=body,
                        _request_timeout=self.api_request_timeout,
                    )
            except Exception as exc:
                if _is_failover_error(exc):
                    raise
                msg = f"Failed to sync ConfigMap {cm_name}: {exc}"
                current_app.logger.error(msg)
                return False, msg

        # prune ConfigMaps that belong to this lab but are no longer attached
        try:
            existing = self.v1_api.list_namespaced_config_map(
                namespace=self.namespace,
                label_selector=f"lab_id={lab_id},hackinsdn.io/labdata=true",
                _request_timeout=self.api_request_timeout,
            )
        except Exception as exc:
            if _is_failover_error(exc):
                raise
            current_app.logger.error(
                f"Failed to list lab data ConfigMaps for lab {lab_id}: {exc}"
            )
            return True, "synced (pruning skipped)"
        for cm in existing.items:
            name = cm.metadata.name
            if name in desired:
                continue
            try:
                self.v1_api.delete_namespaced_config_map(
                    name=name, namespace=self.namespace,
                    _request_timeout=self.api_request_timeout,
                )
            except Exception as exc:
                if _is_failover_error(exc):
                    raise
                current_app.logger.error(
                    f"Failed to delete orphan lab data ConfigMap {name}: {exc}"
                )
        return True, "ok"

    @with_failover(default=(False, "Failed to delete lab data: all kubeconfigs unreachable"))
    def delete_labdata_configmaps(self, lab_id):
        """Delete every lab-data ConfigMap belonging to a lab."""
        try:
            self.v1_api.delete_collection_namespaced_config_map(
                namespace=self.namespace,
                label_selector=f"lab_id={lab_id},hackinsdn.io/labdata=true",
                _request_timeout=self.api_request_timeout,
            )
        except Exception as exc:
            if _is_failover_error(exc):
                raise
            msg = f"Failed to delete lab data ConfigMaps for lab {lab_id}: {exc}"
            current_app.logger.error(msg)
            return False, msg
        return True, "ok"

    def validate_token(self, token):
        """Check if this token is authorized to access the API."""
        return True

    def get_pods_by_lab_id(self, lab_id):
        """Return all pods with a certain lab_id label."""
        return []

    @with_failover
    def get_pod_by_name(self, pod):
        """Return pod by its name."""
        pod = self.v1_api.read_namespaced_pod(
            name=pod["name"], namespace=self.namespace,
            _request_timeout=self.api_request_timeout,
        )
        pod_dict = pod.to_dict()
        pod_dict["is_ok"] = pod.status.phase == "Running"
        return pod_dict

    @with_failover
    def get_deployment_by_name(self, deployment):
        """Return deployment by its name."""
        deployment = self.apps_v1_api.read_namespaced_deployment(
            name=deployment["name"], namespace=self.namespace,
            _request_timeout=self.api_request_timeout,
        )
        dep_dict = deployment.to_dict()
        dep_dict["is_ok"] = deployment.status.replicas == deployment.status.ready_replicas
        return dep_dict

    @with_failover
    def get_service_by_name(self, service):
        """Return service by its name."""
        service = self.v1_api.read_namespaced_service(
            name=service["name"], namespace=self.namespace,
            _request_timeout=self.api_request_timeout,
        )
        service_dict = service.to_dict()
        service_dict["is_ok"] = True
        return service_dict

    @with_failover
    def get_config_map_by_name(self, config_map):
        """Return config_map by its name."""
        config_map = self.v1_api.read_namespaced_config_map(
            name=config_map["name"], namespace=self.namespace,
            _request_timeout=self.api_request_timeout,
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

    @with_failover(default=False)
    def delete_pod_by_name(self, pod):
        """Delete pod by its name."""
        try:
            self.v1_api.delete_namespaced_pod(
                name=pod["name"], namespace=self.namespace,
                _request_timeout=self.api_request_timeout,
            )
        except Exception as exc:
            if _is_failover_error(exc):
                raise
            current_app.logger.warning(f"Failed to delete pod {pod['name']} {exc}")
            return False
        return True

    @with_failover(default=False)
    def delete_deployment_by_name(self, deployment):
        """Delete deployment by its name."""
        try:
            self.apps_v1_api.delete_namespaced_deployment(
                name=deployment["name"], namespace=self.namespace,
                _request_timeout=self.api_request_timeout,
            )
        except Exception as exc:
            if _is_failover_error(exc):
                raise
            current_app.logger.warning(f"Failed to delete deployment {deployment['name']} {exc}")
            return False
        return True

    @with_failover(default=False)
    def delete_service_by_name(self, service):
        """Delete service by its name."""
        try:
            self.v1_api.delete_namespaced_service(
                name=service["name"], namespace=self.namespace,
                _request_timeout=self.api_request_timeout,
            )
        except Exception as exc:
            if _is_failover_error(exc):
                raise
            current_app.logger.warning(f"Failed to delete service {service['name']} {exc}")
            return False
        return True

    @with_failover(default=False)
    def delete_config_map_by_name(self, config_map):
        """Delete config_map by its name."""
        try:
            self.v1_api.delete_namespaced_config_map(
                name=config_map["name"], namespace=self.namespace,
                _request_timeout=self.api_request_timeout,
            )
        except Exception as exc:
            if _is_failover_error(exc):
                raise
            current_app.logger.warning(f"Failed to delete configmap {config_map['name']} {exc}")
            return False
        return True

    @with_failover(default=False)
    def delete_secret_by_name(self, secret):
        """Delete secret by its name."""
        name = secret["name"] if isinstance(secret, dict) else secret
        try:
            self.v1_api.delete_namespaced_secret(
                name=name, namespace=self.namespace,
                _request_timeout=self.api_request_timeout,
            )
        except Exception as exc:
            if _is_failover_error(exc):
                raise
            current_app.logger.warning(f"Failed to delete secret {name}: {exc}")
            return False
        return True

    def delete_resources_by_name(self, resources):
        """Delete resources by their name and kind."""
        results = []
        for resource in reversed(resources):
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
                "latitude": self.k8s_nodes_geotag.get(name, {}).get("lat", 0.0),
                "longitude": self.k8s_nodes_geotag.get(name, {}).get("lng", 0.0),
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

        stats = {
            "total_cpu_capacity": total_cpu_capacity,
            "total_memory_capacity": self.humanbytes(total_memory_capacity),
            "total_storage_capacity": self.humanbytes(total_storage_capacity),
            "total_pods": total_pods,
            "total_nodes": total_nodes,
        }
        # only present when more than one kubeconfig is configured
        failover = self.get_failover_stats()
        if failover:
            stats["kubeconfig"] = failover
        return stats

    @with_failover
    def get_pod_exec_stream(self, pod, container, start_script=None):
        if start_script is None:
            start_script = 'if [ -x /bin/bash ]; then exec /bin/bash; else exec /bin/sh; fi'
        return stream(
            self.v1_api.connect_get_namespaced_pod_exec,
            pod,
            self.namespace,
            command=['sh', '-c', start_script],
            container=container,
            stderr=True,
            stdin=True,
            stdout=True,
            tty=True,
            _preload_content=False,
        )
