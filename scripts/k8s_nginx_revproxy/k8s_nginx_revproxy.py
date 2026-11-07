#!/usr/bin/env python3
"""
k8s_nginx_revproxy.py

Standalone daemon that publishes Kubernetes NodePort services through an
external nginx reverse proxy (HTTPS front-end, backed by a wildcard TLS
certificate and a wildcard DNS record that already exist).

On every poll it:

  1. Lists all Services of type NodePort and all Pods (via the Kubernetes API).
  2. Matches Services to Pods (service.spec.selector  <->  pod.metadata.labels).
  3. For each matched, running+ready Pod it reads status.hostIP and combines it
     with the Service's nodePort to build a set of concrete upstreams
     (hostIP:nodePort). Traffic is thus sent straight to the node(s) actually
     running the backing pods.
  4. Renders one nginx `server{}` (vhost) + `upstream{}` per exposed Service,
     with server_name = <hostname-template> (covered by the wildcard cert/DNS).
  5. Atomically writes the vhosts, runs `nginx -t`, and reloads nginx only when
     the desired configuration actually changed (rolling back on a bad config).
  6. (Optional) When --allow-cidr is set, creates/updates a Kubernetes
     NetworkPolicy per exposed Service so its pods accept ingress only from the
     given CIDRs -- i.e. only through this proxy. The policy is removed when the
     vhost goes away, and is garbage-collected via ownerReferences if the
     Service itself is deleted.

Robustness:
  * Multiple kubeconfigs for the same cluster (different control-plane nodes)
    are cycled through whenever the current API endpoint errors or times out.
  * Every API call has connect/read timeouts.
  * A failed/partial fetch never rewrites nginx -- the last good config stays
    in place until a full, successful fetch is obtained again.
  * Designed to run forever under systemd with Restart=on-failure.

Only depends on the official `kubernetes` python client (and nginx on PATH).
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "The 'kubernetes' python client is required: pip install kubernetes\n"
    )
    raise

try:
    # urllib3 is a transitive dep of the kubernetes client; used to catch
    # connect/read timeouts and connection errors distinctly.
    from urllib3.exceptions import HTTPError as Urllib3HTTPError
except ImportError:  # pragma: no cover
    Urllib3HTTPError = Exception  # type: ignore


log = logging.getLogger("k8s-revproxy")

# NetworkPolicies this tool creates carry this label so they can be listed and
# cleaned up without touching policies managed by anyone else.
MANAGED_BY_KEY = "app.kubernetes.io/managed-by"
MANAGED_BY_VAL = "k8s-nginx-revproxy"

# Fixed annotation (independent of --annotation-prefix) that marks a Service as
# "never reverse-proxy this", in both opt-in and opt-out modes.
SKIP_ANNOTATION = "hackinsdn.ufba.br/skip-revproxy"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _normalize_cidr(value: str) -> str:
    """Accept a CIDR or a bare IP; return a canonical CIDR (bare IPs become
    /32 for IPv4 or /128 for IPv6). Raises ValueError on malformed input."""
    v = value.strip()
    if "/" not in v:
        v = v + ("/128" if ":" in v else "/32")
    # Validate (and canonicalise the network address, e.g. 10.0.0.5/24 kept as
    # given but checked); strict=False allows host bits to be set.
    ipaddress.ip_network(v, strict=False)
    return v


@dataclass
class Settings:
    # One or more kubeconfig files pointing at the *same* cluster through
    # different control-plane endpoints. They are tried in round-robin order,
    # rotating whenever the active one errors out.
    kubeconfigs: List[str] = field(default_factory=list)

    # How often (seconds) to reconcile.
    poll_interval: float = 30.0

    # (connect, read) timeout for every API request.
    connect_timeout: float = 5.0
    read_timeout: float = 15.0

    # Optional proxy for all API traffic, e.g. a SOCKS5 SSH tunnel
    # (socks5h://127.0.0.1:1080). Empty means direct connection.
    proxy: str = ""

    # Directory that nginx `include`s, e.g.:
    #   include /etc/nginx/k8s-revproxy/*.conf;
    # It is treated as owned by this tool -- stale *.conf are removed.
    output_dir: str = "/etc/nginx/k8s-revproxy"

    # Wildcard TLS material (already issued for *.<domain>).
    ssl_certificate: str = "/etc/ssl/certs/wildcard.crt"
    ssl_certificate_key: str = "/etc/ssl/private/wildcard.key"

    # Domain under which every vhost is published. Each hostname is built as
    #   <port>-<service.metadata.name>.<PROXY_DOMAIN>
    # PROXY_DOMAIN must be covered by the wildcard cert/DNS record.
    proxy_domain: str = ""

    # Default scheme used to reach the NodePort backends. It is http unless the
    # service port name starts with "https" (then https), and can still be
    # forced per-service with the <prefix>/scheme annotation.
    default_upstream_scheme: str = "http"

    # nginx binary + reload command.
    nginx_bin: str = "nginx"
    reload_cmd: List[str] = field(
        default_factory=lambda: ["systemctl", "reload", "nginx"]
    )

    # Annotation prefix for per-service overrides, e.g.
    #   revproxy.hackinsdn.io/hostname: myapp.<domain>   (overrides the template)
    #   revproxy.hackinsdn.io/scheme: https              (overrides port-name rule)
    #   revproxy.hackinsdn.io/port: "8443"   (service port name or number)
    #   revproxy.hackinsdn.io/enable: "false"
    # A separate fixed annotation, hackinsdn.ufba.br/skip-revproxy: "true",
    # marks a service as never-proxy (see SKIP_ANNOTATION).
    annotation_prefix: str = "revproxy.hackinsdn.io"

    # If set, only expose services carrying <annotation_prefix>/enable=true.
    # Otherwise every NodePort service is exposed unless it opts out with
    # <annotation_prefix>/enable=false.
    opt_in: bool = False

    # Require pods to be Ready (not merely Running) before routing to them.
    require_ready: bool = True

    # Namespace(s) to watch. At least one must be provided.
    namespaces: List[str] = field(default_factory=list)

    # CIDRs allowed to reach the exposed pods. When non-empty, a NetworkPolicy
    # is created per exposed Service restricting ingress to these CIDRs (i.e.
    # only through this proxy). Empty disables NetworkPolicy management entirely.
    allow_cidrs: List[str] = field(default_factory=list)

    # When True (and lockdown is on), patch each locked-down Service to
    # externalTrafficPolicy: Local so the pods see the real client source IP and
    # the NetworkPolicy allow-list can match. Meant to be enabled temporarily to
    # migrate services; mutates user-owned Services and needs services/patch RBAC.
    patch_external_traffic_policy: bool = False

    # Only print the nginx config that would be written; never touch disk,
    # nginx -t, or reload. Implies a single cycle.
    dry_run: bool = False

    def ann(self, suffix: str) -> str:
        return f"{self.annotation_prefix}/{suffix}"


def parse_args(argv: Optional[List[str]] = None) -> Settings:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kubeconfig", action="append", default=[],
                   help="Path to a kubeconfig (repeatable). Also KUBECONFIGS=path1:path2.")
    p.add_argument("--poll-interval", type=float, default=float(_env("POLL_INTERVAL", "30")))
    p.add_argument("--connect-timeout", type=float, default=float(_env("CONNECT_TIMEOUT", "5")))
    p.add_argument("--read-timeout", type=float, default=float(_env("READ_TIMEOUT", "15")))
    p.add_argument("--proxy", default=_env("PROXY", "") or _env("HTTPS_PROXY", ""),
                   help="Proxy for all API traffic, e.g. socks5h://127.0.0.1:1080 "
                        "for a SOCKS5 SSH tunnel. Also PROXY / HTTPS_PROXY env.")
    p.add_argument("--output-dir", default=_env("OUTPUT_DIR", "/etc/nginx/k8s-revproxy"))
    p.add_argument("--ssl-certificate", default=_env("SSL_CERTIFICATE", "/etc/ssl/certs/wildcard.crt"))
    p.add_argument("--ssl-certificate-key", default=_env("SSL_CERTIFICATE_KEY", "/etc/ssl/private/wildcard.key"))
    p.add_argument("--proxy-domain", default=_env("PROXY_DOMAIN", ""),
                   help="Domain for published vhosts (also PROXY_DOMAIN env). "
                        "Hostnames are <port>-<service>.<PROXY_DOMAIN>.")
    p.add_argument("--upstream-scheme", default=_env("UPSTREAM_SCHEME", "http"), choices=["http", "https"])
    p.add_argument("--nginx-bin", default=_env("NGINX_BIN", "nginx"))
    p.add_argument("--reload-cmd", default=_env("RELOAD_CMD", "systemctl reload nginx"),
                   help="Shell-word list, e.g. 'systemctl reload nginx' or 'nginx -s reload'.")
    p.add_argument("--annotation-prefix", default=_env("ANNOTATION_PREFIX", "revproxy.hackinsdn.io"))
    p.add_argument("--opt-in", action="store_true", default=_env("OPT_IN", "").lower() in ("1", "true", "yes"))
    p.add_argument("--no-require-ready", dest="require_ready", action="store_false",
                   default=_env("REQUIRE_READY", "true").lower() in ("1", "true", "yes"))
    p.add_argument("--namespace", action="append", default=[],
                   help="Namespace to watch (repeatable). Also NAMESPACES=ns1,ns2 (or NAMESPACE=ns).")
    p.add_argument("--allow-cidr", action="append", default=[],
                   help="CIDR (or bare IP) allowed to reach the exposed pods via a "
                        "NetworkPolicy, so the NodePort service is reachable only "
                        "through this proxy. Repeatable. Also ALLOW_CIDRS=cidr1,cidr2. "
                        "When unset, no NetworkPolicy is managed.")
    p.add_argument("--patch-external-traffic-policy", action="store_true",
                   default=_env("PATCH_EXTERNAL_TRAFFIC_POLICY", "").lower() in ("1", "true", "yes"),
                   help="Temporarily patch each locked-down Service to "
                        "externalTrafficPolicy: Local (needed for the NetworkPolicy "
                        "allow-list to match proxy traffic). Only acts when "
                        "--allow-cidr is set; mutates Services and needs "
                        "services/patch RBAC. Remove once services are migrated. "
                        "Also PATCH_EXTERNAL_TRAFFIC_POLICY env.")
    p.add_argument("--log-level", default=_env("LOG_LEVEL", "INFO"))
    p.add_argument("--once", action="store_true", help="Run a single reconcile and exit (for testing).")
    p.add_argument("--dry-run", action="store_true",
                   default=_env("DRY_RUN", "").lower() in ("1", "true", "yes"),
                   help="Print the nginx config that would be written and exit; "
                        "no disk writes, no nginx -t, no reload.")
    a = p.parse_args(argv)

    kubeconfigs = list(a.kubeconfig)
    if not kubeconfigs and _env("KUBECONFIGS", ""):
        kubeconfigs = [x for x in _env("KUBECONFIGS", "").split(os.pathsep) if x]
    if not kubeconfigs:
        p.error("at least one --kubeconfig (or KUBECONFIGS env) is required")

    namespaces = list(a.namespace)
    if not namespaces:
        env_ns = _env("NAMESPACES", "") or _env("NAMESPACE", "")
        namespaces = [x.strip() for x in re.split(r"[,\s]+", env_ns) if x.strip()]
    if not namespaces:
        p.error("at least one --namespace (or NAMESPACE/NAMESPACES env) is required")

    if not a.proxy_domain:
        p.error("--proxy-domain (or PROXY_DOMAIN env) is required")

    allow_cidrs_raw = list(a.allow_cidr)
    if not allow_cidrs_raw and _env("ALLOW_CIDRS", ""):
        allow_cidrs_raw = [x.strip() for x in re.split(r"[,\s]+", _env("ALLOW_CIDRS", "")) if x.strip()]
    allow_cidrs: List[str] = []
    for raw in allow_cidrs_raw:
        try:
            allow_cidrs.append(_normalize_cidr(raw))
        except ValueError:
            p.error(f"invalid --allow-cidr value: {raw!r}")

    s = Settings(
        kubeconfigs=kubeconfigs,
        poll_interval=a.poll_interval,
        connect_timeout=a.connect_timeout,
        read_timeout=a.read_timeout,
        proxy=a.proxy,
        output_dir=a.output_dir,
        ssl_certificate=a.ssl_certificate,
        ssl_certificate_key=a.ssl_certificate_key,
        proxy_domain=a.proxy_domain,
        default_upstream_scheme=a.upstream_scheme,
        nginx_bin=a.nginx_bin,
        reload_cmd=a.reload_cmd.split(),
        annotation_prefix=a.annotation_prefix,
        opt_in=a.opt_in,
        require_ready=a.require_ready,
        namespaces=namespaces,
        allow_cidrs=allow_cidrs,
        patch_external_traffic_policy=a.patch_external_traffic_policy,
        dry_run=a.dry_run,
    )
    logging.basicConfig(
        level=getattr(logging, a.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    s._once = a.once  # type: ignore[attr-defined]
    return s


# --------------------------------------------------------------------------- #
# Kubernetes client pool (round-robin over kubeconfigs on failure)
# --------------------------------------------------------------------------- #
class KubeClientPool:
    """Builds an ApiClient from the current kubeconfig (shared by the Core and
    Networking API wrappers) and rotates to the next kubeconfig whenever
    :meth:`rotate` is called (i.e. the current endpoint failed)."""

    def __init__(self, kubeconfigs: List[str], proxy: str = ""):
        if not kubeconfigs:
            raise ValueError("no kubeconfigs provided")
        self._kubeconfigs = kubeconfigs
        self._proxy = proxy
        self._idx = 0
        self._client: Optional["client.ApiClient"] = None

    @property
    def current_path(self) -> str:
        return self._kubeconfigs[self._idx]

    def _build(self) -> "client.ApiClient":
        cfg = client.Configuration()
        # Isolate each kubeconfig into its own Configuration/ApiClient so
        # rotating never leaks state (host, token, CA) between endpoints.
        config.load_kube_config(config_file=self.current_path, client_configuration=cfg)

        use_socks = bool(self._proxy) and self._proxy.lower().startswith("socks")
        if self._proxy and not use_socks:
            # Plain http/https proxy: the client handles this natively.
            cfg.proxy = self._proxy

        api_client = client.ApiClient(cfg)
        if use_socks:
            # The kubernetes client passes Configuration.proxy to
            # urllib3.ProxyManager, which rejects socks schemes
            # (ProxySchemeUnknown). Swap in a SOCKSProxyManager instead,
            # mirroring the TLS settings the client would have used.
            self._install_socks_pool_manager(api_client, cfg)

        log.info("Using kubeconfig %s (host=%s)%s", self.current_path, cfg.host,
                 f" via proxy {self._proxy}" if self._proxy else "")
        return api_client

    def _install_socks_pool_manager(self, api_client, cfg) -> None:
        import ssl as _ssl
        try:
            from urllib3.contrib.socks import SOCKSProxyManager
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "SOCKS proxy requested but PySocks is missing; "
                "install it with: pip install 'urllib3[socks]'"
            ) from exc

        maxsize = cfg.connection_pool_maxsize or 4
        pool_args = {
            "num_pools": maxsize,
            "maxsize": maxsize,
            "cert_reqs": _ssl.CERT_REQUIRED if cfg.verify_ssl else _ssl.CERT_NONE,
            "ca_certs": cfg.ssl_ca_cert,
            "cert_file": cfg.cert_file,
            "key_file": cfg.key_file,
        }
        if getattr(cfg, "assert_hostname", None) is not None:
            pool_args["assert_hostname"] = cfg.assert_hostname

        rest = api_client.rest_client
        try:
            rest.pool_manager.clear()  # drop the direct-connection pool
        except Exception:  # noqa: BLE001
            pass
        rest.pool_manager = SOCKSProxyManager(self._proxy, **pool_args)

    def _api_client(self) -> "client.ApiClient":
        if self._client is None:
            self._client = self._build()
        return self._client

    def api(self) -> client.CoreV1Api:
        return client.CoreV1Api(self._api_client())

    def networking(self) -> client.NetworkingV1Api:
        return client.NetworkingV1Api(self._api_client())

    def rotate(self) -> None:
        prev = self.current_path
        self._idx = (self._idx + 1) % len(self._kubeconfigs)
        self._client = None  # rebuilt lazily on next api()/networking()
        if len(self._kubeconfigs) > 1:
            log.warning("Rotating kubeconfig %s -> %s", prev, self.current_path)


# --------------------------------------------------------------------------- #
# Domain model
# --------------------------------------------------------------------------- #
@dataclass
class VHost:
    hostname: str
    upstream_name: str
    scheme: str
    endpoints: List[str]  # ["10.0.0.5:31234", ...]

    def sort_key(self) -> str:
        return self.hostname


@dataclass
class NetworkPolicySpec:
    """A NetworkPolicy to create for a proxied Service: lock its pods down so
    they accept ingress only from the configured CIDRs."""
    namespace: str
    name: str            # == Service name (also the policy name)
    uid: str             # Service uid, for the ownerReference
    selector: Dict[str, str]  # == Service spec.selector
    # Set when --patch-external-traffic-policy is on and the Service is not yet
    # Local; drives a one-off patch before the policy is applied. Not part of the
    # rendered policy, so it is excluded from config_hash / to_body.
    needs_etp_patch: bool = False

    def config_hash(self, cidrs: List[str]) -> str:
        """Stable short hash of everything that affects the rendered policy, so
        reconcile only rewrites a policy when its selector or CIDRs change."""
        h = hashlib.sha256()
        for k in sorted(self.selector):
            h.update(f"{k}={self.selector[k]}".encode())
            h.update(b"\0")
        h.update(b"|")
        for c in sorted(cidrs):
            h.update(c.encode())
            h.update(b"\0")
        return h.hexdigest()[:16]

    def to_body(self, cidrs: List[str], hash_annotation: str) -> dict:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": self.name,
                "namespace": self.namespace,
                "labels": {MANAGED_BY_KEY: MANAGED_BY_VAL},
                "annotations": {hash_annotation: self.config_hash(cidrs)},
                "ownerReferences": [{
                    "apiVersion": "v1",
                    "kind": "Service",
                    "name": self.name,
                    "uid": self.uid,
                }],
            },
            "spec": {
                "podSelector": {"matchLabels": dict(self.selector)},
                "policyTypes": ["Ingress"],
                "ingress": [{
                    "from": [{"ipBlock": {"cidr": c}} for c in cidrs],
                }],
            },
        }


@dataclass
class Desired:
    """The desired state computed from one fetch: nginx vhosts plus the
    NetworkPolicies (keyed by (namespace, name)) to reconcile."""
    vhosts: List[VHost] = field(default_factory=list)
    netpols: Dict[Tuple[str, str], NetworkPolicySpec] = field(default_factory=dict)


_SAFE = re.compile(r"[^a-zA-Z0-9]+")


def _safe_name(*parts: str) -> str:
    return _SAFE.sub("_", "_".join(parts)).strip("_").lower()


def _sanitize_host(name: str) -> str:
    # DNS label(s): lowercase, only [a-z0-9.-].
    return re.sub(r"[^a-z0-9.-]+", "-", name.lower()).strip("-.")


def _pod_matches_selector(pod, selector: Dict[str, str]) -> bool:
    labels = pod.metadata.labels or {}
    return all(labels.get(k) == v for k, v in selector.items())


def _pod_is_ready(pod) -> bool:
    conds = (pod.status.conditions or []) if pod.status else []
    for c in conds:
        if c.type == "Ready":
            return c.status == "True"
    return False


def _annotation(obj, key: str) -> Optional[str]:
    anns = (obj.metadata.annotations or {}) if obj.metadata else {}
    return anns.get(key)


def _is_true(val: Optional[str]) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Reconciler
# --------------------------------------------------------------------------- #
class Reconciler:
    def __init__(self, settings: Settings):
        self.s = settings
        self.pool = KubeClientPool(settings.kubeconfigs, proxy=settings.proxy)
        self._last_written_hash: Optional[str] = None
        # NetworkPolicy management state:
        #   _np_disabled     -> set once if we lack RBAC to manage policies
        #   _np_cleanup_done -> the one-shot cleanup run when the feature is off
        self._np_disabled = False
        self._np_cleanup_done = False
        # Services (ns, name) already warned about a non-Local
        # externalTrafficPolicy, so we complain once rather than every cycle.
        self._etp_warned: set = set()
        # Set once if we lack RBAC to patch Services, so --patch-external-
        # traffic-policy doesn't retry (and log) on every cycle.
        self._etp_patch_disabled = False

    # ---- fetch ---------------------------------------------------------- #
    def _list_with_rotation(self, fn, *, what: str, **kwargs):
        """Call an API list function; on failure rotate kubeconfig and raise so
        the caller aborts this cycle without touching nginx."""
        timeout = (self.s.connect_timeout, self.s.read_timeout)
        try:
            return fn(_request_timeout=timeout, **kwargs)
        except (ApiException, Urllib3HTTPError, OSError) as exc:
            log.error("Failed to list %s via %s: %s", what, self.pool.current_path, exc)
            self.pool.rotate()
            raise

    def fetch_desired(self) -> Desired:
        api = self.pool.api()
        services = []
        pods = []
        for ns in self.s.namespaces:
            services += self._list_with_rotation(
                api.list_namespaced_service, what=f"services in {ns}", namespace=ns
            ).items
            pods += self._list_with_rotation(
                api.list_namespaced_pod, what=f"pods in {ns}", namespace=ns
            ).items

        # Index running pods by namespace for quick selector matching.
        pods_by_ns: Dict[str, list] = {}
        for pod in pods:
            if not pod.status or pod.status.phase != "Running":
                continue
            if not pod.status.host_ip:
                continue
            if self.s.require_ready and not _pod_is_ready(pod):
                continue
            pods_by_ns.setdefault(pod.metadata.namespace, []).append(pod)

        desired = Desired()
        vhosts: Dict[str, VHost] = {}
        for svc in services:
            vh, npspec = self._service_to_vhosts(svc, pods_by_ns)
            for v in vh:
                if v.hostname in vhosts:
                    log.warning("Hostname collision for %s -- keeping first, "
                                "skipping service %s/%s", v.hostname,
                                svc.metadata.namespace, svc.metadata.name)
                    continue
                vhosts[v.hostname] = v
            if npspec is not None:
                desired.netpols[(npspec.namespace, npspec.name)] = npspec
        desired.vhosts = sorted(vhosts.values(), key=VHost.sort_key)
        return desired

    def _service_to_vhosts(self, svc, pods_by_ns) -> Tuple[List[VHost], Optional[NetworkPolicySpec]]:
        s = self.s
        ns = svc.metadata.namespace
        name = svc.metadata.name

        if not svc.spec or svc.spec.type != "NodePort":
            return [], None

        # Explicit opt-out: the fixed skip-revproxy annotation wins in both
        # opt-in and opt-out modes, for services you never want proxied
        # regardless of the global policy.
        if _is_true(_annotation(svc, SKIP_ANNOTATION)):
            log.debug("Service %s/%s skipped via %s annotation", ns, name, SKIP_ANNOTATION)
            return [], None

        enable = _annotation(svc, s.ann("enable"))
        if s.opt_in:
            if (enable or "").lower() != "true":
                return [], None
        else:
            if (enable or "").lower() == "false":
                log.debug("Service %s/%s opted out via annotation", ns, name)
                return [], None

        selector = svc.spec.selector or {}
        if not selector:
            log.debug("Service %s/%s has no selector; skipping", ns, name)
            return [], None

        # Ports that actually have a nodePort assigned.
        node_ports = [p for p in (svc.spec.ports or []) if p.node_port]
        if not node_ports:
            return [], None

        # Optional per-service port restriction.
        want_port = _annotation(svc, s.ann("port"))
        if want_port:
            node_ports = [
                p for p in node_ports
                if str(p.port) == want_port or (p.name and p.name == want_port)
                or str(p.node_port) == want_port
            ]
            if not node_ports:
                log.warning("Service %s/%s: annotated port %r has no nodePort",
                            ns, name, want_port)
                return [], None

        # Matching, ready pods -> the set of node IPs to route to.
        matched = [pod for pod in pods_by_ns.get(ns, [])
                   if _pod_matches_selector(pod, selector)]
        if not matched:
            log.debug("Service %s/%s has no ready backing pods; skipping", ns, name)
            return [], None
        host_ips = sorted({pod.status.host_ip for pod in matched})

        # Per-service scheme/hostname overrides (optional).
        ann_scheme = (_annotation(svc, s.ann("scheme")) or "").lower()
        ann_host = _annotation(svc, s.ann("hostname"))

        out: List[VHost] = []
        for p in node_ports:
            portname = p.name or str(p.port)
            proto = (p.protocol or "TCP").upper()

            # Only TCP can be reverse-proxied at L7 (http/https); skip the rest.
            if proto != "TCP":
                log.info("Service %s/%s port %s uses protocol %s; skipping "
                         "(only TCP is supported)", ns, name, portname, proto)
                continue

            # hostname = <port>-<service>.<PROXY_DOMAIN>, annotation wins.
            hostname = ann_host or f"{p.port}-{name}.{s.proxy_domain}"
            hostname = _sanitize_host(hostname)

            # scheme: http by default; https if the port name starts with
            # "https"; annotation overrides everything.
            if ann_scheme in ("http", "https"):
                scheme = ann_scheme
            elif portname.lower().startswith("https"):
                scheme = "https"
            else:
                scheme = s.default_upstream_scheme

            endpoints = [f"{ip}:{p.node_port}" for ip in host_ips]
            out.append(VHost(
                hostname=hostname,
                upstream_name=_safe_name(ns, name, portname),
                scheme=scheme,
                endpoints=endpoints,
            ))

        # One NetworkPolicy per Service that got at least one vhost, locking the
        # backing pods to the configured CIDRs. Only when the feature is on.
        npspec = None
        if out and s.allow_cidrs:
            etp = getattr(svc.spec, "external_traffic_policy", None)
            not_local = etp != "Local"
            # Flip the service to Local ourselves (patch mode), or just warn.
            patch = bool(not_local and s.patch_external_traffic_policy)
            if not_local and not s.patch_external_traffic_policy:
                self._warn_if_not_local_etp(ns, name, etp)
            npspec = NetworkPolicySpec(
                namespace=ns,
                name=name,
                uid=svc.metadata.uid or "",
                selector=dict(selector),
                needs_etp_patch=patch,
            )
        return out, npspec

    def _warn_if_not_local_etp(self, ns: str, name: str, etp) -> None:
        """Warn (once per service) when we are about to lock a NodePort service
        down with a NetworkPolicy but its externalTrafficPolicy is not Local.

        A NetworkPolicy ipBlock matches the source IP as the pod sees it. With
        the default externalTrafficPolicy: Cluster, NodePort traffic is SNAT'd
        to the receiving node's IP, so the pod never sees the proxy's real
        address: the allow-list cannot match proxy traffic, and it cannot tell
        proxy traffic apart from a direct attacker hitting the NodePort (both
        arrive as the node IP). externalTrafficPolicy: Local preserves the real
        source IP and is effectively required for the lockdown to work.
        """
        key = (ns, name)
        if key in self._etp_warned:
            return
        self._etp_warned.add(key)
        log.warning(
            "Service %s/%s has externalTrafficPolicy=%s; the NetworkPolicy "
            "allow-list will not match proxy traffic (NodePort SNATs the source "
            "IP to the node). Set externalTrafficPolicy: Local on the Service "
            "(or run with --patch-external-traffic-policy) to preserve the "
            "client source IP, or the lockdown will drop/mis-match traffic. "
            "See the README NetworkPolicy lockdown section.",
            ns, name, etp or "Cluster")

    # ---- render --------------------------------------------------------- #
    def render(self, vhosts: List[VHost]) -> Dict[str, str]:
        """Return {filename: content} for the managed output directory."""
        files: Dict[str, str] = {}
        # A single map file (needs http{} context, once) for websocket upgrades.
        files["00-maps.conf"] = (
            "# Managed by k8s_nginx_revproxy.py -- do not edit.\n"
            "map $http_upgrade $revproxy_connection_upgrade {\n"
            "    default upgrade;\n"
            "    ''      close;\n"
            "}\n"
        )
        for v in vhosts:
            files[f"{v.hostname}.conf"] = self._render_vhost(v)
        return files

    def _render_vhost(self, v: VHost) -> str:
        s = self.s
        servers = "\n".join(f"    server {ep} max_fails=3 fail_timeout=15s;"
                            for ep in v.endpoints)
        ssl_verify = "" if v.scheme == "http" else "        proxy_ssl_verify off;\n"
        return f"""# Managed by k8s_nginx_revproxy.py -- do not edit.
upstream {v.upstream_name} {{
{servers}
}}

server {{
    listen 80;
    listen [::]:80;
    server_name {v.hostname};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {v.hostname};

    ssl_certificate     {s.ssl_certificate};
    ssl_certificate_key {s.ssl_certificate_key};

    location / {{
        proxy_pass {v.scheme}://{v.upstream_name};
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        $revproxy_connection_upgrade;
{ssl_verify}        proxy_connect_timeout 5s;
        proxy_next_upstream error timeout http_502 http_503 http_504;
    }}
}}
"""

    # ---- apply ---------------------------------------------------------- #
    @staticmethod
    def _hash(files: Dict[str, str]) -> str:
        h = hashlib.sha256()
        for name in sorted(files):
            h.update(name.encode())
            h.update(b"\0")
            h.update(files[name].encode())
            h.update(b"\0")
        return h.hexdigest()

    def _read_current(self) -> Dict[str, str]:
        current: Dict[str, str] = {}
        d = self.s.output_dir
        if not os.path.isdir(d):
            return current
        for fn in os.listdir(d):
            if fn.endswith(".conf"):
                try:
                    with open(os.path.join(d, fn), "r", encoding="utf-8") as fh:
                        current[fn] = fh.read()
                except OSError as exc:
                    log.warning("Could not read %s: %s", fn, exc)
        return current

    def _write_files(self, files: Dict[str, str]) -> None:
        d = self.s.output_dir
        os.makedirs(d, exist_ok=True)
        # Write/replace desired files atomically.
        for fn, content in files.items():
            path = os.path.join(d, fn)
            tmp = f"{path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, path)
        # Remove stale files we no longer manage.
        for fn in os.listdir(d):
            if fn.endswith(".conf") and fn not in files:
                try:
                    os.remove(os.path.join(d, fn))
                except OSError as exc:
                    log.warning("Could not remove stale %s: %s", fn, exc)

    def _nginx_test(self) -> bool:
        try:
            r = subprocess.run([self.s.nginx_bin, "-t"],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.error("nginx -t failed to run: %s", exc)
            return False
        if r.returncode != 0:
            log.error("nginx -t rejected config:\n%s", r.stderr.strip())
            return False
        return True

    def _reload(self) -> bool:
        try:
            r = subprocess.run(self.s.reload_cmd, capture_output=True,
                               text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.error("nginx reload failed to run: %s", exc)
            return False
        if r.returncode != 0:
            log.error("nginx reload failed:\n%s", r.stderr.strip())
            return False
        return True

    def apply(self, files: Dict[str, str]) -> bool:
        """Write/reload nginx. Returns True when the config is live and matches
        `files` (including the no-change / already-up-to-date cases), False when
        nginx rejected it (rolled back) or the reload failed."""
        desired_hash = self._hash(files)
        if desired_hash == self._last_written_hash:
            log.debug("No configuration change (%d vhosts).", len(files) - 1)
            return True

        previous = self._read_current()
        if self._hash(previous) == desired_hash:
            # On-disk already matches (e.g. first run after restart).
            self._last_written_hash = desired_hash
            log.info("Configuration already up to date (%d vhosts).", len(files) - 1)
            return True

        log.info("Applying configuration: %d vhost(s).", len(files) - 1)
        added = sorted(k for k in files if k not in previous and k != "00-maps.conf")
        removed = sorted(k for k in previous if k not in files and k != "00-maps.conf")
        for fn in added:
            log.info("New vhost: %s", fn[:-len(".conf")] if fn.endswith(".conf") else fn)
        for fn in removed:
            log.info("Removed vhost: %s", fn[:-len(".conf")] if fn.endswith(".conf") else fn)
        self._write_files(files)
        if not self._nginx_test():
            log.error("Rolling back to previous configuration.")
            self._write_files(previous)  # restore exact previous state
            self._nginx_test()           # best-effort validation of rollback
            return False
        if self._reload():
            self._last_written_hash = desired_hash
            log.info("nginx reloaded successfully.")
            return True
        log.error("Reload failed; leaving new files in place for inspection.")
        return False

    # ---- network policies ---------------------------------------------- #
    def reconcile_network_policies(
        self, desired: Dict[Tuple[str, str], NetworkPolicySpec]
    ) -> bool:
        """Ensure exactly the desired set of managed NetworkPolicies exists:
        create missing ones, update changed ones, delete ones no longer wanted.
        Only the policies this tool owns (managed-by label) are ever touched.
        An empty `desired` therefore removes every managed policy -- which is how
        dropping --allow-cidr tears the lockdown back down.

        Returns True when it successfully listed and reconciled, False when it
        skipped (e.g. no RBAC) so the caller can retry later."""
        networking = self.pool.networking()
        cidrs = self.s.allow_cidrs
        hash_ann = self.s.ann("config-hash")

        existing = self._list_managed_netpols(networking)
        if existing is None:
            return False  # could not list (no RBAC / API error) -- skip quietly

        # Create / update.
        for key, spec in sorted(desired.items()):
            ns, name = key
            # Patch mode: flip the Service to externalTrafficPolicy: Local before
            # locking it down, so the allow-list can actually match proxy traffic.
            if spec.needs_etp_patch:
                self._patch_service_etp_local(ns, name)
            body = spec.to_body(cidrs, hash_ann)
            cur = existing.get(key)
            if cur is None:
                self._np_create(networking, ns, name, body)
            else:
                cur_hash = (cur.metadata.annotations or {}).get(hash_ann)
                if cur_hash != spec.config_hash(cidrs):
                    body["metadata"]["resourceVersion"] = cur.metadata.resource_version
                    self._np_replace(networking, ns, name, body)

        # Delete managed policies no longer desired.
        for key in sorted(existing):
            if key not in desired:
                self._np_delete(networking, key[0], key[1])
        return True

    def _list_managed_netpols(self, networking):
        """Return {(ns, name): policy} for the policies this tool manages, or
        None if they can't be listed (missing RBAC disables NP management for
        the rest of the run; a connectivity error rotates + raises to retry)."""
        label_sel = f"{MANAGED_BY_KEY}={MANAGED_BY_VAL}"
        existing: Dict[Tuple[str, str], object] = {}
        for ns in self.s.namespaces:
            try:
                resp = networking.list_namespaced_network_policy(
                    namespace=ns, label_selector=label_sel,
                    _request_timeout=self._np_timeout())
            except ApiException as exc:
                if getattr(exc, "status", None) in (401, 403):
                    log.warning("No RBAC to list NetworkPolicies (status %s); "
                                "NetworkPolicy management disabled for this run. "
                                "Grant get/list/create/update/delete on "
                                "networkpolicies to manage or clean them up.",
                                exc.status)
                    self._np_disabled = True
                else:
                    log.error("Listing NetworkPolicies in %s failed: %s", ns, exc)
                return None
            except (Urllib3HTTPError, OSError) as exc:
                log.error("Listing NetworkPolicies in %s failed (connectivity): %s", ns, exc)
                self.pool.rotate()
                raise
            for np in resp.items:
                existing[(ns, np.metadata.name)] = np
        return existing

    def _np_timeout(self):
        return (self.s.connect_timeout, self.s.read_timeout)

    def _np_api_error(self, verb: str, ns: str, name: str, exc) -> None:
        if getattr(exc, "status", None) in (401, 403):
            log.error("NetworkPolicy %s %s/%s forbidden (status %s); check RBAC "
                      "(needs get/list/create/update/delete on networkpolicies).",
                      verb, ns, name, exc.status)
        else:
            log.error("NetworkPolicy %s %s/%s failed: %s", verb, ns, name, exc)

    def _np_create(self, networking, ns: str, name: str, body: dict) -> None:
        try:
            networking.create_namespaced_network_policy(
                namespace=ns, body=body, _request_timeout=self._np_timeout())
            log.info("Created NetworkPolicy %s/%s", ns, name)
        except ApiException as exc:
            if exc.status == 409:
                log.warning("NetworkPolicy %s/%s already exists and is not managed "
                            "by this tool; leaving it untouched.", ns, name)
            else:
                self._np_api_error("create", ns, name, exc)
        except (Urllib3HTTPError, OSError) as exc:
            log.error("NetworkPolicy create %s/%s failed (connectivity): %s", ns, name, exc)
            self.pool.rotate()
            raise

    def _np_replace(self, networking, ns: str, name: str, body: dict) -> None:
        try:
            networking.replace_namespaced_network_policy(
                name=name, namespace=ns, body=body, _request_timeout=self._np_timeout())
            log.info("Updated NetworkPolicy %s/%s", ns, name)
        except ApiException as exc:
            self._np_api_error("replace", ns, name, exc)
        except (Urllib3HTTPError, OSError) as exc:
            log.error("NetworkPolicy replace %s/%s failed (connectivity): %s", ns, name, exc)
            self.pool.rotate()
            raise

    def _np_delete(self, networking, ns: str, name: str) -> None:
        try:
            networking.delete_namespaced_network_policy(
                name=name, namespace=ns, _request_timeout=self._np_timeout())
            log.info("Removed NetworkPolicy %s/%s", ns, name)
        except ApiException as exc:
            if exc.status == 404:
                log.debug("NetworkPolicy %s/%s already gone.", ns, name)
            else:
                self._np_api_error("delete", ns, name, exc)
        except (Urllib3HTTPError, OSError) as exc:
            log.error("NetworkPolicy delete %s/%s failed (connectivity): %s", ns, name, exc)
            self.pool.rotate()
            raise

    def _patch_service_etp_local(self, ns: str, name: str) -> None:
        """Patch a Service to externalTrafficPolicy: Local (one-off migration
        for --patch-external-traffic-policy). Idempotent: once patched, the next
        fetch sees Local and stops requesting the patch. Uses a JSON Patch so the
        client sends application/json-patch+json (a dict body would be mis-sent
        as JSON Patch and rejected)."""
        if self._etp_patch_disabled:
            return
        api = self.pool.api()
        patch = [{"op": "add", "path": "/spec/externalTrafficPolicy", "value": "Local"}]
        try:
            api.patch_namespaced_service(
                name=name, namespace=ns, body=patch,
                _request_timeout=self._np_timeout())
            log.warning("Patched Service %s/%s externalTrafficPolicy -> Local "
                        "(--patch-external-traffic-policy); remove the flag once "
                        "all target services are migrated.", ns, name)
        except ApiException as exc:
            if getattr(exc, "status", None) in (401, 403):
                log.error("No RBAC to patch Service %s/%s externalTrafficPolicy "
                          "(status %s); service patching disabled for this run. "
                          "Grant patch on services to use "
                          "--patch-external-traffic-policy.", ns, name, exc.status)
                self._etp_patch_disabled = True
            else:
                log.error("Patching Service %s/%s externalTrafficPolicy failed: %s",
                          ns, name, exc)
        except (Urllib3HTTPError, OSError) as exc:
            log.error("Patching Service %s/%s failed (connectivity): %s", ns, name, exc)
            self.pool.rotate()
            raise

    def dry_run(self, files: Dict[str, str],
                netpols: Dict[Tuple[str, str], NetworkPolicySpec]) -> None:
        """Print the nginx config (and any NetworkPolicies) that would be
        written, without touching disk, nginx -t, reload, or the cluster."""
        sys.stdout.write(
            f"# ===== dry-run: {len(files)} nginx file(s) would be written to "
            f"{self.s.output_dir} =====\n"
        )
        for fn in sorted(files):
            sys.stdout.write(f"\n# ----- {os.path.join(self.s.output_dir, fn)} -----\n")
            sys.stdout.write(files[fn])

        if netpols:
            try:
                import yaml  # pyyaml ships with the kubernetes client
                def _dump(b):
                    return yaml.safe_dump(b, default_flow_style=False, sort_keys=False)
            except ImportError:
                import json
                def _dump(b):
                    return json.dumps(b, indent=2) + "\n"
            sys.stdout.write(f"\n# ===== dry-run: {len(netpols)} NetworkPolicy(ies) "
                             f"would be applied =====\n")
            hash_ann = self.s.ann("config-hash")
            for key in sorted(netpols):
                body = netpols[key].to_body(self.s.allow_cidrs, hash_ann)
                sys.stdout.write("\n---\n")
                sys.stdout.write(_dump(body))
        sys.stdout.flush()

    # ---- one cycle ------------------------------------------------------ #
    def reconcile_once(self) -> None:
        desired = self.fetch_desired()  # raises on API failure (nginx untouched)
        files = self.render(desired.vhosts)
        if self.s.dry_run:
            self.dry_run(files, desired.netpols)
            return
        applied_ok = self.apply(files)
        # Only touch NetworkPolicies when nginx is healthy -- never lock a
        # service down behind a proxy that failed to come up.
        if applied_ok and not self._np_disabled:
            if self.s.allow_cidrs:
                # Feature on: create/update desired, delete managed strays.
                self.reconcile_network_policies(desired.netpols)
            elif not self._np_cleanup_done:
                # Feature off: run once to remove any policies we created on a
                # previous run (i.e. --allow-cidr was dropped). `desired` is
                # empty, so every managed policy is deleted.
                if self.reconcile_network_policies({}):
                    self._np_cleanup_done = True


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
_stop = False


def _handle_signal(signum, _frame):
    global _stop
    log.info("Received signal %s, shutting down.", signum)
    _stop = True


def main(argv: Optional[List[str]] = None) -> int:
    settings = parse_args(argv)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    rec = Reconciler(settings)
    once = getattr(settings, "_once", False) or settings.dry_run
    backoff = settings.poll_interval

    log.info("Starting with %d kubeconfig(s), namespaces=%s, domain=%s, poll=%ss, output=%s",
             len(settings.kubeconfigs), ",".join(settings.namespaces),
             settings.proxy_domain, settings.poll_interval, settings.output_dir)
    if settings.allow_cidrs:
        log.info("NetworkPolicy lockdown enabled; allowed CIDRs: %s",
                 ", ".join(settings.allow_cidrs))
        if settings.patch_external_traffic_policy:
            log.warning("Service patching enabled: locked-down services will be "
                        "patched to externalTrafficPolicy: Local. Disable "
                        "(--patch-external-traffic-policy off) once migrated.")

    while not _stop:
        start = time.monotonic()
        try:
            rec.reconcile_once()
            backoff = settings.poll_interval  # reset after a good cycle
        except (ApiException, Urllib3HTTPError, OSError) as exc:
            # fetch_desired already logged + rotated the kubeconfig.
            log.warning("Reconcile cycle failed (%s); nginx config unchanged.", type(exc).__name__)
        except Exception:  # noqa: BLE001  keep the daemon alive; systemd is the safety net
            log.exception("Unexpected error during reconcile; continuing.")

        if once:
            break

        elapsed = time.monotonic() - start
        sleep_for = max(1.0, backoff - elapsed)
        # Interruptible sleep so signals are handled promptly.
        deadline = time.monotonic() + sleep_for
        while not _stop and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    log.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
