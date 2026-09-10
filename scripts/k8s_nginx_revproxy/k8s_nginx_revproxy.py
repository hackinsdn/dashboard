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
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

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


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


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
    annotation_prefix: str = "revproxy.hackinsdn.io"

    # If set, only expose services carrying <annotation_prefix>/enable=true.
    # Otherwise every NodePort service is exposed unless it opts out with
    # <annotation_prefix>/enable=false.
    opt_in: bool = False

    # Require pods to be Ready (not merely Running) before routing to them.
    require_ready: bool = True

    # Namespace(s) to watch. At least one must be provided.
    namespaces: List[str] = field(default_factory=list)

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
    """Builds a CoreV1Api from the current kubeconfig and rotates to the next
    one whenever :meth:`rotate` is called (i.e. the current endpoint failed)."""

    def __init__(self, kubeconfigs: List[str], proxy: str = ""):
        if not kubeconfigs:
            raise ValueError("no kubeconfigs provided")
        self._kubeconfigs = kubeconfigs
        self._proxy = proxy
        self._idx = 0
        self._api: Optional[client.CoreV1Api] = None

    @property
    def current_path(self) -> str:
        return self._kubeconfigs[self._idx]

    def _build(self) -> client.CoreV1Api:
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

        api = client.CoreV1Api(api_client)
        log.info("Using kubeconfig %s (host=%s)%s", self.current_path, cfg.host,
                 f" via proxy {self._proxy}" if self._proxy else "")
        return api

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

    def api(self) -> client.CoreV1Api:
        if self._api is None:
            self._api = self._build()
        return self._api

    def rotate(self) -> None:
        prev = self.current_path
        self._idx = (self._idx + 1) % len(self._kubeconfigs)
        self._api = None  # rebuilt lazily on next api()
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


# --------------------------------------------------------------------------- #
# Reconciler
# --------------------------------------------------------------------------- #
class Reconciler:
    def __init__(self, settings: Settings):
        self.s = settings
        self.pool = KubeClientPool(settings.kubeconfigs, proxy=settings.proxy)
        self._last_written_hash: Optional[str] = None

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

    def fetch_vhosts(self) -> List[VHost]:
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

        vhosts: Dict[str, VHost] = {}
        for svc in services:
            vh = self._service_to_vhosts(svc, pods_by_ns)
            for v in vh:
                if v.hostname in vhosts:
                    log.warning("Hostname collision for %s -- keeping first, "
                                "skipping service %s/%s", v.hostname,
                                svc.metadata.namespace, svc.metadata.name)
                    continue
                vhosts[v.hostname] = v
        return sorted(vhosts.values(), key=VHost.sort_key)

    def _service_to_vhosts(self, svc, pods_by_ns) -> List[VHost]:
        s = self.s
        ns = svc.metadata.namespace
        name = svc.metadata.name

        if not svc.spec or svc.spec.type != "NodePort":
            return []

        enable = _annotation(svc, s.ann("enable"))
        if s.opt_in:
            if (enable or "").lower() != "true":
                return []
        else:
            if (enable or "").lower() == "false":
                log.debug("Service %s/%s opted out via annotation", ns, name)
                return []

        selector = svc.spec.selector or {}
        if not selector:
            log.debug("Service %s/%s has no selector; skipping", ns, name)
            return []

        # Ports that actually have a nodePort assigned.
        node_ports = [p for p in (svc.spec.ports or []) if p.node_port]
        if not node_ports:
            return []

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
                return []

        # Matching, ready pods -> the set of node IPs to route to.
        matched = [pod for pod in pods_by_ns.get(ns, [])
                   if _pod_matches_selector(pod, selector)]
        if not matched:
            log.debug("Service %s/%s has no ready backing pods; skipping", ns, name)
            return []
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
        return out

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

    def apply(self, files: Dict[str, str]) -> None:
        desired_hash = self._hash(files)
        if desired_hash == self._last_written_hash:
            log.debug("No configuration change (%d vhosts).", len(files) - 1)
            return

        previous = self._read_current()
        if self._hash(previous) == desired_hash:
            # On-disk already matches (e.g. first run after restart).
            self._last_written_hash = desired_hash
            log.info("Configuration already up to date (%d vhosts).", len(files) - 1)
            return

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
            return
        if self._reload():
            self._last_written_hash = desired_hash
            log.info("nginx reloaded successfully.")
        else:
            log.error("Reload failed; leaving new files in place for inspection.")

    def dry_run(self, files: Dict[str, str]) -> None:
        """Print the nginx config that would be written, without touching disk,
        nginx -t, or reload."""
        sys.stdout.write(
            f"# ===== dry-run: {len(files)} file(s) would be written to "
            f"{self.s.output_dir} =====\n"
        )
        for fn in sorted(files):
            sys.stdout.write(f"\n# ----- {os.path.join(self.s.output_dir, fn)} -----\n")
            sys.stdout.write(files[fn])
        sys.stdout.flush()

    # ---- one cycle ------------------------------------------------------ #
    def reconcile_once(self) -> None:
        vhosts = self.fetch_vhosts()  # raises on API failure (nginx untouched)
        files = self.render(vhosts)
        if self.s.dry_run:
            self.dry_run(files)
            return
        self.apply(files)


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

    while not _stop:
        start = time.monotonic()
        try:
            rec.reconcile_once()
            backoff = settings.poll_interval  # reset after a good cycle
        except (ApiException, Urllib3HTTPError, OSError) as exc:
            # fetch_vhosts already logged + rotated the kubeconfig.
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
