# k8s_nginx_revproxy

A standalone daemon that automatically publishes Kubernetes **NodePort** services
through an external **nginx** HTTPS reverse proxy.

## What it does and the problem it solves

Services of type `NodePort` are reachable at `<nodeIP>:<nodePort>`, on a random
high port, over plain HTTP, with no friendly name and no TLS. Exposing many such
services to users means either hand-writing an nginx vhost per service (and
keeping it in sync as pods move between nodes) or standing up a full ingress
controller inside the cluster.

This script removes that toil. It runs *outside* the cluster on the box that also
runs nginx, and on a loop it:

1. Lists all `NodePort` services and all pods in the namespaces you configure
   (via the Kubernetes API).
2. Matches each service to its backing pods (`service.spec.selector` ↔
   `pod.metadata.labels`), keeping only pods that are **Running + Ready** and
   have a `status.hostIP`.
3. Builds the concrete upstreams `hostIP:nodePort` — the set of nodes actually
   running the pods — so traffic goes straight to the right node(s), with
   automatic load-balancing and failover when there is more than one.
4. Renders one nginx `server{}` + `upstream{}` per service (HTTP→HTTPS redirect,
   HTTPS front-end using your wildcard certificate, websocket support).
5. Atomically writes the vhosts, runs `nginx -t`, and reloads nginx — **only when
   the desired config actually changed**, rolling back if `nginx -t` rejects it.

It is built to run forever under systemd and to survive Kubernetes hiccups:
every API call is time-bounded, a failed or partial fetch never rewrites nginx
(the last good config stays live), and you can supply **several kubeconfigs for
the same cluster** (pointing at different control-plane nodes) which it cycles
through whenever the current endpoint errors or times out.

### Naming scheme

Each service is published at:

```
<service.spec.ports.port>-<service.metadata.name>.<PROXY_DOMAIN>
```

For example, a service named `webui` in namespace `hackinsdn` exposing port
`8080` becomes:

```
8080-webui.k8s-ingress.example.com
```

This pairs with the dashboard's new **`PROXY_DOMAIN`** setting: the dashboard
builds the same hostname to link users to a deployed service, and this daemon
makes that hostname actually resolve and terminate TLS. Because the name uses a
single DNS label under `PROXY_DOMAIN`, it is covered by a wildcard DNS record and
a wildcard TLS certificate for `*.PROXY_DOMAIN` (see prerequisites).

Only **TCP** ports are published — UDP/SCTP ports can't be reverse-proxied at
L7 and are skipped (logged at INFO). A service can override its hostname, scheme,
or which port is exposed with annotations (see [Annotations](#per-service-annotations)).

## Prerequisites

- **nginx** on the same host, with a place to `include` the generated vhosts.
  For a full tuned & hardened install, follow [`NGINX_SETUP.md`](NGINX_SETUP.md).
  At minimum, add this line inside the `http {}` block of `/etc/nginx/nginx.conf`:

  ```nginx
  include /etc/nginx/k8s-revproxy/*.conf;
  ```

  Because the generated hostnames are long, also raise the server-name hash
  bucket size in the `http {}` context (or a snippet in `conf.d/`):

  ```nginx
  server_names_hash_bucket_size 128;
  ```

- **Wildcard DNS**: an `A`/`AAAA` (or `CNAME`) record for `*.PROXY_DOMAIN`
  pointing at this nginx host, so every `<port>-<service>.PROXY_DOMAIN` resolves.

- **Wildcard TLS certificate** for `*.PROXY_DOMAIN` (e.g. Let's Encrypt DNS-01,
  or any CA). Point `--ssl-certificate` / `--ssl-certificate-key` at the
  fullchain and private key. The certificate and DNS record are assumed to
  already exist — this script does not manage them.

- **Kubeconfig(s) with RBAC access** to *list* services and pods in the target
  namespaces. A read-only ClusterRole/Role is enough:

  ```yaml
  apiVersion: rbac.authorization.k8s.io/v1
  kind: ClusterRole
  metadata:
    name: k8s-nginx-revproxy-reader
  rules:
    - apiGroups: [""]
      resources: ["services", "pods"]
      verbs: ["get", "list"]
    # Only needed when --allow-cidr is set (NetworkPolicy lockdown, see below):
    - apiGroups: ["networking.k8s.io"]
      resources: ["networkpolicies"]
      verbs: ["get", "list", "create", "update", "patch", "delete"]
  ```

  Bind it to the identity used by each kubeconfig. Provide one kubeconfig per
  control-plane endpoint for failover. The `networkpolicies` rule can be omitted
  if you never use `--allow-cidr`.

- **Python 3.9+** and the dependencies in `requirements.txt`
  (`kubernetes`; plus `urllib3[socks]` / PySocks only if you use a SOCKS proxy).

## Configuration

All options can be given as a command-line flag or an environment variable; the
flag wins when both are set. Three options are **required**: `--kubeconfig`,
`--namespace`, and `--proxy-domain`.

| Flag | Env var | Default | Description |
|------|---------|---------|-------------|
| `--kubeconfig` (repeatable) | `KUBECONFIGS` (`:`-separated) | — **required** | Kubeconfig file(s) for the same cluster; cycled on failure. |
| `--namespace` (repeatable) | `NAMESPACES` (comma/space) or `NAMESPACE` | — **required** | Namespace(s) to watch. |
| `--proxy-domain` | `PROXY_DOMAIN` | — **required** | Domain for published vhosts. Hostnames are `<port>-<service>.<PROXY_DOMAIN>`. |
| `--poll-interval` | `POLL_INTERVAL` | `30` | Seconds between reconciles. |
| `--connect-timeout` | `CONNECT_TIMEOUT` | `5` | Per-request connect timeout (seconds). |
| `--read-timeout` | `READ_TIMEOUT` | `15` | Per-request read timeout (seconds). |
| `--proxy` | `PROXY` or `HTTPS_PROXY` | *(none)* | Proxy for all API traffic, e.g. `socks5h://127.0.0.1:1080` for a SOCKS5 SSH tunnel. |
| `--output-dir` | `OUTPUT_DIR` | `/etc/nginx/k8s-revproxy` | Directory the tool owns and writes vhosts into (stale `*.conf` are removed). |
| `--ssl-certificate` | `SSL_CERTIFICATE` | `/etc/ssl/certs/wildcard.crt` | Wildcard TLS certificate (fullchain). |
| `--ssl-certificate-key` | `SSL_CERTIFICATE_KEY` | `/etc/ssl/private/wildcard.key` | Wildcard TLS private key. |
| `--upstream-scheme` | `UPSTREAM_SCHEME` | `http` | Default scheme to reach backends (`http` or `https`). Auto-upgrades to `https` when the service port name starts with `https`. |
| `--nginx-bin` | `NGINX_BIN` | `nginx` | nginx binary used for `nginx -t`. |
| `--reload-cmd` | `RELOAD_CMD` | `systemctl reload nginx` | Command to reload nginx (shell words). |
| `--annotation-prefix` | `ANNOTATION_PREFIX` | `revproxy.hackinsdn.io` | Prefix for per-service annotation overrides. |
| `--opt-in` | `OPT_IN` (`1`/`true`/`yes`) | `false` | Expose only services annotated `<prefix>/enable=true`. Otherwise every NodePort service is exposed unless it sets `<prefix>/enable=false`. |
| `--no-require-ready` | `REQUIRE_READY` (`false` to disable) | ready required | Route to Running pods even if not Ready. |
| `--allow-cidr` (repeatable) | `ALLOW_CIDRS` (comma/space) | *(none)* | CIDR (or bare IP, normalized to `/32` / `/128`) allowed to reach the exposed pods via a per-service NetworkPolicy, so the NodePort service is reachable only through this proxy. When unset, no NetworkPolicy is managed. See [NetworkPolicy lockdown](#networkpolicy-lockdown). |
| `--log-level` | `LOG_LEVEL` | `INFO` | Python logging level. |
| `--dry-run` | `DRY_RUN` (`1`/`true`/`yes`) | `false` | Print the config that would be written and exit; no disk writes, no `nginx -t`, no reload. Implies a single cycle. |
| `--once` | — | `false` | Run a single reconcile and exit (for testing). |

### Per-service annotations

Set on a Service to override defaults (prefix configurable via
`--annotation-prefix`, default `revproxy.hackinsdn.io`):

| Annotation | Effect |
|------------|--------|
| `<prefix>/enable` | `"true"` to expose (when `--opt-in`), or `"false"` to opt out. |
| `<prefix>/hostname` | Override the full published hostname. |
| `<prefix>/scheme` | Force the upstream scheme (`http` or `https`). |
| `<prefix>/port` | Expose only the given service port (name or number). |

## NetworkPolicy lockdown

By default the daemon only publishes a reverse-proxy vhost; the underlying
NodePort is still reachable directly on every node. Passing one or more
`--allow-cidr` values turns on **NetworkPolicy lockdown**: after a service's
vhost is live, the daemon creates a `NetworkPolicy` that restricts ingress to
the backing pods to just those CIDRs — so the service is reachable only through
this proxy. The CIDRs must be the source the pods actually see for proxied
traffic, which requires `externalTrafficPolicy: Local` on the Service; see
[Preserving the client source IP](#preserving-the-client-source-ip-read-this-before-enabling)
below before enabling.

For a service named `webui` with selector `app=webui`, the rendered policy is:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: webui
  labels:
    app.kubernetes.io/managed-by: k8s-nginx-revproxy
  annotations:
    revproxy.hackinsdn.io/config-hash: <hash>
  ownerReferences:
    - apiVersion: v1
      kind: Service
      name: webui
      uid: <service-uid>
spec:
  podSelector:
    matchLabels:
      app: webui
  policyTypes: [Ingress]
  ingress:
    - from:
        - ipBlock: {cidr: 10.0.0.1/32}
        - ipBlock: {cidr: 192.168.0.0/24}
```

How it behaves:

- **One policy per proxied Service**, named after the service, with
  `podSelector` = the service's `spec.selector` and one `ipBlock` per allowed
  CIDR. Bare IPs are normalized (`/32` for IPv4, `/128` for IPv6).
- **Created only after a successful nginx apply** — the daemon never locks a
  service down behind a proxy that failed to come up.
- **Reconciled idempotently**: policies carry a config-hash annotation, so a
  cycle only rewrites one when its selector or the allowed CIDRs change. They're
  tagged `app.kubernetes.io/managed-by: k8s-nginx-revproxy`, and only policies
  with that label are ever touched — a same-named policy you created by hand is
  left alone (logged as a warning).
- **Cleaned up** when a service stops being proxied (its vhost is removed, so is
  its policy). The `ownerReferences` entry also lets Kubernetes garbage-collect
  the policy automatically if the Service itself is deleted. A delete that finds
  the policy already gone is treated as success.
- **Removing `--allow-cidr` tears the lockdown down.** On its next run with the
  flag unset, the daemon does a one-time pass that deletes every policy it
  manages (the `managed-by` label), so services become directly reachable
  again. This still needs the `networkpolicies` RBAC verbs; if they've also been
  revoked, the daemon logs a warning and leaves the policies for you (or the
  `ownerReferences` GC) to remove.
- Needs the `networking.k8s.io/networkpolicies` RBAC verbs (see
  [Prerequisites](#prerequisites)), and **a CNI that enforces NetworkPolicy**
  (Calico, Cilium, etc.) — on a cluster without one the policy is accepted but
  has no effect.

### Preserving the client source IP (read this before enabling)

A NetworkPolicy `ipBlock` matches the **source IP as the pod's CNI sees it on
the inbound packet** — not the original client address. This has a decisive
consequence for NodePort services:

- With the default **`externalTrafficPolicy: Cluster`**, a packet arriving at
  `nodeA:nodePort` is **SNAT'd to `nodeA`'s own IP** before it reaches the pod
  (so the reply path stays symmetric). The pod never sees the proxy's real IP,
  so an allow-list of proxy CIDRs **never matches** and all proxied traffic is
  dropped. Allow-listing the node IPs instead does not help: a direct attacker
  hitting the same NodePort is *also* SNAT'd to the node IP, so the policy can't
  tell the proxy apart from the attacker. Under `Cluster` the lockdown cannot do
  its job.
- With **`externalTrafficPolicy: Local`**, kube-proxy routes the NodePort only
  to pods **on the same node** and does **not** SNAT, so the pod sees the real
  client (proxy) source IP and the allow-list works. Because this daemon already
  points nginx at the node that hosts the pod (`hostIP:nodePort`), that node
  always has a local pod, so `Local`'s usual "drop if no local pod" blackhole
  does not apply to the proxy path.

**So set `externalTrafficPolicy: Local` on every Service you lock down.** The
daemon reads but does not own your Services, so you (or the service authors) must
set this; when the daemon is about to create a policy for a service that is still
`Cluster`, it logs a one-time warning per service. Example:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: webui
spec:
  type: NodePort
  externalTrafficPolicy: Local
  selector:
    app: webui
  ports:
    - port: 8080
      nodePort: 31080
```

Further caveats to verify in your environment:

- **Allow-list the source IP the *node* actually sees**, not the IP you assume
  the proxy has. If anything between the proxy and the nodes does NAT (a router,
  a gateway, a different egress interface than the API/SSH-tunnel path), the pod
  sees that translated address instead. Confirm empirically (see below) rather
  than assuming it is the nginx box's own IP.
- **Watch for CNI re-masquerading.** Even with `Local`, some CNIs masquerade
  traffic whose source is outside the cluster/pod CIDR (Calico `natOutgoing`,
  Cilium masquerade, `ip-masq-agent` ranges). If enabled, the pod can still see a
  rewritten source. Check your CNI's masquerade settings.
- **Don't block health probes / legitimate in-cluster clients.** A policy that
  allows only the proxy CIDR denies everything else. Kubelet probes usually come
  from the node and most CNIs exempt them, but behavior varies — verify probes
  and any intra-cluster callers still pass after locking down.
- **Pod rescheduling.** The upstream node IPs are cached per reconcile cycle;
  under `Local`, if a pod moves to another node between cycles the old node has
  no local pod and briefly drops traffic until the next cycle re-renders nginx.
  Keep the reconcile interval short if this matters.

To verify what the pod actually sees, hit a service through the proxy and check:

```bash
# if the app logs client IPs:
kubectl -n <ns> logs <pod>
# or capture directly:
kubectl -n <ns> exec <pod> -- sh -c 'timeout 10 tcpdump -ni any tcp and port <containerPort>'
```

Then confirm the lockdown actually locks: from a host **not** in the allow-list,
`curl http://<nodeIP>:<nodePort>` should fail, while the proxy still works.

Preview the policies without applying anything with `--dry-run`, which prints
the rendered NetworkPolicy YAML after the nginx config.

## Running it manually

Install the dependencies (a virtualenv is recommended):

```bash
python3 -m venv /opt/k8s-nginx-revproxy
/opt/k8s-nginx-revproxy/bin/pip install -r requirements.txt
```

Preview what would be generated, without touching nginx:

```bash
env PROXY_DOMAIN=k8s-ingress.example.com NAMESPACES=hackinsdn \
    /opt/k8s-nginx-revproxy/bin/python3 k8s_nginx_revproxy.py \
    --kubeconfig /etc/k8s-nginx-revproxy/kubeconfig-node1.yaml \
    --kubeconfig /etc/k8s-nginx-revproxy/kubeconfig-node2.yaml \
    --ssl-certificate /etc/letsencrypt/live/k8s-ingress.example.com/fullchain.pem \
    --ssl-certificate-key /etc/letsencrypt/live/k8s-ingress.example.com/privkey.pem \
    --dry-run
```

Drop `--dry-run` to actually write vhosts, validate, reload nginx, and keep
reconciling every `--poll-interval` seconds. Add `--once` to run a single cycle.

### Through a SOCKS5 SSH tunnel

If the API servers are only reachable via a jump host, open a tunnel and point
`--proxy` at it (use `socks5h` so DNS resolves on the remote side):

```bash
ssh -f -N -D 127.0.0.1:1080 user@jump-host
# ... --proxy socks5h://127.0.0.1:1080
```

This requires PySocks (`pip install 'urllib3[socks]'`). The tunnel covers only
the Kubernetes API traffic; nginx still reaches `hostIP:nodePort` over the normal
network.

Please check the `ssh-socks-tunnel.service` for an example on how to setup the SSH
tunnel managed by systemd.

## Installing as a systemd service

The unit file `k8s-nginx-revproxy.service` is included. It runs the daemon on a
loop with `Restart=on-failure` and basic sandboxing.

```bash
# 1. Install the code and dependencies
sudo mkdir -p /opt/k8s-nginx-revproxy /etc/k8s-nginx-revproxy /etc/nginx/k8s-revproxy
sudo cp k8s_nginx_revproxy.py /opt/k8s-nginx-revproxy/
sudo python3 -m venv /opt/k8s-nginx-revproxy
sudo /opt/k8s-nginx-revproxy/bin/pip install -r requirements.txt

# 2. Drop your kubeconfigs into /etc/k8s-nginx-revproxy/
#    (kubeconfig-node1.yaml, kubeconfig-node2.yaml, ...)

# 3. Install and edit the unit (set PROXY_DOMAIN, NAMESPACES, cert paths, kubeconfigs)
sudo cp k8s-nginx-revproxy.service /etc/systemd/system/
sudo systemctl edit --full k8s-nginx-revproxy.service   # or edit the file directly

# 4. Make sure nginx includes the vhost directory (see Prerequisites), then:
sudo systemctl daemon-reload
sudo systemctl enable --now k8s-nginx-revproxy.service
sudo systemctl status k8s-nginx-revproxy.service
journalctl -u k8s-nginx-revproxy.service -f
```

If you route the API through an SSH tunnel, run the tunnel as its own unit (e.g.
with `autossh`) and uncomment the `After=`/`Wants=` lines and the `PROXY=` env in
the unit so the proxy starts only once the tunnel is up.

## Files

| File | Purpose |
|------|---------|
| `k8s_nginx_revproxy.py` | The daemon. |
| `requirements.txt` | Python dependencies. |
| `k8s-nginx-revproxy.service` | systemd unit template. |
| `NGINX_SETUP.md` | Tuned & hardened nginx install guide (Debian 12). |
| `ssh-socks-tunnel.service` | systemd unit template for the optional SOCKS5 SSH tunnel. |
| `README.md` | This document. |

Tests live at `tests/test_k8s_nginx_revproxy.py` (run from the repo root with
`pytest tests/test_k8s_nginx_revproxy.py`); they stub the Kubernetes client, so
no cluster is needed.
