# Design: Multi-kubeconfig failover for the Kubernetes controller

> **Status: implemented.** `apps/config.py` (`K8S_CONFIGS`, `K8S_FLAP_THRESHOLD`),
> `apps/controllers/kubernetes.py` (`_KubeEndpoint`, `with_failover`,
> `_is_failover_error`/`_reason_for`, `_FailoverError`, client properties,
> failover metrics + `get_statistics` block), `env-template`, and tests
> (`tests/test_k8s_failover.py` plus fixture updates in
> `tests/test_k8s_controller.py` / `tests/test_k8s_tape.py`). One
> implementation refinement vs. this doc: the kubectl/best-effort helpers raise
> a typed `_FailoverError` carrying the friendly message (so it survives
> exhaustion), and `with_failover(default=...)` lets the delete/secret helpers
> keep their historical "return False on total failure" contract.

## Goal

Let `K8sController` (`apps/controllers/kubernetes.py`) work against **several
kubeconfig files** that are redundant access paths to the **same logical
cluster** (HA endpoints / credentials). At any moment exactly one kubeconfig is
*active*. When a call through the active kubeconfig fails for a **connectivity
or auth** reason (timeout, connection refused, API server 5xx / unreachable, or
`401`/`403` from an expired/invalid credential in that kubeconfig), the
controller switches to the next kubeconfig and retries. It **sticks** with a
working kubeconfig until that one fails, then circles to the next, wrapping
around the list.

### Decisions (locked in with the requester)

| Question | Decision |
|---|---|
| Topology | **Same cluster (HA).** Kubeconfigs are interchangeable; state (pods/labs/nodes) is shared, so failover is transparent and no per-cluster ownership tracking is needed. |
| Config format | **Comma-separated list of kubeconfig file paths** in the existing `K8S_CONFIG`/`KUBECONFIG` env var. |
| Failover trigger | **Connectivity + auth.** Timeouts, connection errors, API status `0`/`None`, HTTP `5xx`, **and** auth failures (`401` Unauthorized / `403` Forbidden) — an HA path may carry an expired/invalid credential, so try the next one. Other API errors (`400/404/409/422/...`) mean the cluster answered a valid request → **no** failover, propagate as today. |
| Failback | **None.** Sticky: stay on the current working kubeconfig until it fails. No background re-probe of the preferred one. |

### Non-goals

- No cross-cluster reconciliation (topology is single-cluster HA).
- No automatic return to a "primary" kubeconfig (explicitly out of scope).
- No change to the on-disk manifest / lab lifecycle logic.

## Current state (what the change touches)

The controller builds its clients **once** in `__init__`
([kubernetes.py:32-52](../apps/controllers/kubernetes.py)):

```python
config.load_kube_config(config_file=app_config.K8S_CONFIG)
self.v1_api        = client.CoreV1Api()
self.apps_v1_api   = client.AppsV1Api()
self.discovery_api = client.DiscoveryV1Api()
self.k8s_client    = client.ApiClient()
```

Two API surfaces must be routed to the active kubeconfig:

1. **python `kubernetes` client** — `self.v1_api`, `self.apps_v1_api`,
   `self.discovery_api`, `self.k8s_client`, and the exec `stream()` in
   `get_pod_exec_stream`. Used in ~40 call sites, always with
   `_request_timeout=self.request_timeout`.
2. **`kubectl` subprocess** — `get_k8s_resource`, `create_k8s_resource`
   (fallback branch), `delete_k8s_resource`. These currently pass **no**
   `--kubeconfig` and rely on the ambient `~/.kube/config`. They must be
   pinned to the active kubeconfig too, or they'd silently target a different
   endpoint than the python client.

Runtime shape that constrains the design:

- **Process model:** gunicorn `-w 1 --worker-class gevent --threads 128`
  (`docker-entrypoint.sh`). One process, one shared singleton controller
  (`_LazyProxy` in `apps/controllers/__init__.py`), many concurrent greenlets.
  The active-kubeconfig switch is **shared mutable state** → needs a lock and a
  compare-and-swap rotation to avoid a thundering herd skipping past healthy
  endpoints.
- **Disabled state:** when `K8S_NAMESPACE` is empty or no kubeconfig loads,
  today `self.v1_api is None` and callers short-circuit
  (`if not self.v1_api:` in `get_labs_by_user`). This must be preserved.
- **Tests:** `tests/test_k8s_controller.py` assigns mocks directly onto the
  client object (`ctrl.v1_api.read_namespaced_pod = MagicMock(...)`) and later
  reads `ctrl.v1_api.list_namespaced_pod.call_args`. So `self.v1_api` **must
  keep returning a stable, real client object** — a "magic attribute proxy"
  that rewrites every attribute access would break these. This is the key
  constraint that selects the design below.

## Design

### 1. Configuration

`apps/config.py`:

```python
# unchanged env var; now parsed as a comma-separated list
_raw = os.getenv("KUBECONFIG", "~/.kube/config")
K8S_CONFIGS = [
    os.path.expanduser(p.strip()) for p in _raw.split(",") if p.strip()
]
# backward-compat alias: first entry, still a single path
K8S_CONFIG = K8S_CONFIGS[0] if K8S_CONFIGS else ""
```

- A single path (today's usage) → one-element list → behaves exactly as before
  (loop of length 1, failover never triggers). **Fully backward compatible.**
- Comma is the separator (not `:`), because the python `kubernetes` library and
  `kubectl` treat a **colon**-separated `KUBECONFIG` as *files to merge into one
  config* — the opposite of what we want (we want them kept separate and tried
  one at a time). Documented in `env-template`.

### 2. A `_KubeEndpoint` per kubeconfig

Encapsulate one kubeconfig and its (lazily built) clients. Each endpoint owns an
**isolated** `client.Configuration` so switching never mutates a global/default
config shared with another endpoint:

```python
class _KubeEndpoint:
    def __init__(self, path):
        self.path = path
        self._built = False
        self.v1_api = self.apps_v1_api = self.discovery_api = self.k8s_client = None

    def build(self):                 # called under the controller lock
        if self._built:
            return
        cfg = client.Configuration()
        config.load_kube_config(config_file=self.path, client_configuration=cfg)
        api = client.ApiClient(configuration=cfg)
        self.v1_api        = client.CoreV1Api(api)
        self.apps_v1_api   = client.AppsV1Api(api)
        self.discovery_api = client.DiscoveryV1Api(api)
        self.k8s_client    = api
        self._built = True
```

- **Lazy build:** standby kubeconfigs aren't loaded until first used, so a
  broken/absent standby file doesn't cost anything at startup and only surfaces
  when we actually fail over to it.
- Endpoints whose `build()` raises at startup are still kept in the list
  (they may recover); a build failure during failover is treated as a
  connectivity failure and we move to the next endpoint.

### 3. Active-endpoint selection + `self.v1_api` as properties

The controller keeps the ordered list and an active index, guarded by a lock
(`threading.Lock`; gevent monkeypatches `threading`, so it's cooperative):

```python
self._endpoints  = [_KubeEndpoint(p) for p in app_config.K8S_CONFIGS]
self._active_idx = 0
self._lock       = threading.Lock()
```

The four client attributes become **read-only properties** that return the
**active** endpoint's real client (or `None` when disabled). Call sites stay
byte-for-byte unchanged (`self.v1_api.list_namespaced_pod(...)`), and because a
property returns the same underlying client object for a given active endpoint,
the existing tests' `ctrl.v1_api.method = MagicMock()` / `.call_args` pattern
keeps working:

```python
@property
def v1_api(self):
    ep = self._active_endpoint()      # None if disabled → `if not self.v1_api` still works
    return ep.v1_api if ep else None
# ...same for apps_v1_api, discovery_api, k8s_client

def _active_endpoint(self):
    if not self._endpoints:
        return None
    ep = self._endpoints[self._active_idx]
    if not ep._built:
        with self._lock:
            ep.build()
    return ep
```

> **Disabled state:** if `K8S_NAMESPACE` is empty or `K8S_CONFIGS` is empty, we
> leave `self._endpoints = []` so every property returns `None` — identical to
> today's `self.v1_api = None`.

### 4. Failover: rotate + retry

A decorator wraps the controller methods that talk to the cluster. On a
**failover error** (connectivity or auth, per §6) it advances the active
endpoint (compare-and-swap) and re-invokes the method against the new active
endpoint. It stops after trying each endpoint at most once and re-raises the
last error if all fail:

```python
def with_failover(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        attempts = max(1, len(self._endpoints))
        last_exc = None
        for _ in range(attempts):
            ep = self._active_endpoint()
            try:
                return method(self, *args, **kwargs)
            except Exception as exc:
                if not _is_failover_error(exc):
                    raise                      # valid API error → propagate as today
                last_exc = exc
                self._record_failover(ep, exc)  # metric + WARNING log (see §8)
                self._rotate_from(ep)           # sticky advance, CAS-guarded
        raise last_exc
    return wrapper

def _rotate_from(self, failed_ep):
    with self._lock:
        # only advance if nobody else already moved us off the failed endpoint
        if self._endpoints[self._active_idx] is failed_ep:
            self._active_idx = (self._active_idx + 1) % len(self._endpoints)
```

**Where the decorator goes — granularity matters:**

- **Idempotent reads** (`list_pods`, `list_deployments`, `list_services`,
  `get_lab_resources`, `get_labs_by_user`, `update_nodes`, the `*_by_name`
  getters): decorate freely. A mid-way failover just repeats read work against
  the sibling endpoint — harmless.
- **Single-resource writes** — decorate the **innermost** call, not the
  orchestration. Decorate `create_k8s_resource`, `delete_k8s_resource`,
  `get_k8s_resource`, `create_registry_secret`, and the individual
  `delete_*_by_name`. **Do NOT** decorate `create_lab`: it loops creating many
  docs; if endpoint A dies after creating docs #1–#2, retrying the *whole*
  `create_lab` on endpoint B (same cluster) would hit `409 AlreadyExists` on
  those docs. By decorating `create_k8s_resource` instead, only the failing doc
  is retried on B and the loop continues — the lab still completes. A `409` on
  retry is *not* a failover error, so it propagates into `create_lab`'s
  existing rollback path (`delete_resources_by_name` + `_wait_gone`) — safe
  either way.

**Sticky semantics:** the decorator never resets `_active_idx` to 0. Once
endpoint _k_ works, it stays active for all subsequent calls until _k_ itself
throws a connectivity error. That is exactly "stay on the one that worked until
it fails, then circle."

**Thundering herd:** under gevent, many greenlets can fail on endpoint _k_ at
once. `_rotate_from` only advances when the active endpoint is *still* the one
that failed, so the fleet advances **exactly one** step to _k+1_ regardless of
how many callers observed the failure — they don't skip past healthy endpoints.

### 5. `kubectl` subprocess routing

Centralize the three kubectl call sites through one helper that (a) injects
`--kubeconfig <active path>` and (b) does the same connectivity-vs-command-error
classification, so kubectl fails over in lockstep with the python client:

```python
def _run_kubectl(self, args, **run_kwargs):
    ep = self._active_endpoint()
    cmd = ["kubectl", "--kubeconfig", ep.path, *args]
    return subprocess.run(cmd, timeout=self.request_timeout, **run_kwargs)
```

`get_k8s_resource` / `create_k8s_resource` / `delete_k8s_resource` call
`_run_kubectl(...)` and are wrapped with `@with_failover`. Classification for
kubectl:

- **Connectivity → failover:** `subprocess.TimeoutExpired`, or
  `CalledProcessError` whose stderr matches `Unable to connect to the server`,
  `connection refused`, `i/o timeout`, `TLS handshake timeout`,
  `no route to host`, `EOF`.
- **Command error → propagate:** `NotFound`, `AlreadyExists`, validation
  errors, etc. (raised/returned exactly as today).

`_is_failover_error` handles both worlds (python-client exceptions and the
kubectl exceptions surfaced by these helpers).

### 6. `_is_failover_error` (connectivity + auth)

The classifier fails over on both transport-level failures **and** auth
failures (`401`/`403`) — these HA paths can carry different credentials, so an
expired/invalid token on one path is a reason to try the next. Every other
`ApiException` (a valid request the cluster answered: `400/404/409/422/...`)
propagates unchanged:

```python
_AUTH_STATUSES = (401, 403)

def _is_failover_error(exc):
    import socket, urllib3, subprocess
    from kubernetes.client.exceptions import ApiException
    if isinstance(exc, ApiException):
        status = exc.status or 0
        return status in (0,) or status in _AUTH_STATUSES or status >= 500
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    if isinstance(exc, subprocess.CalledProcessError):
        return _kubectl_stderr_is_failover(exc.stderr)   # connectivity OR auth
    return isinstance(exc, (
        urllib3.exceptions.MaxRetryError,
        urllib3.exceptions.ReadTimeoutError,
        urllib3.exceptions.ConnectTimeoutError,
        urllib3.exceptions.ProtocolError,
        socket.timeout, TimeoutError, ConnectionError,
    ))
```

- `_kubectl_stderr_is_failover` matches the connectivity strings from §5 **plus**
  auth strings kubectl emits: `Unauthorized`, `error: You must be logged in`,
  `the server has asked for the client to provide credentials`, `forbidden`.
- The classifier is named `_is_failover_error` throughout (not
  `_is_connectivity_error`) to reflect that auth is a first-class trigger.

> **Guarding against auth-flap loops:** if *all* kubeconfigs return `401`
> (e.g. a cluster-wide RBAC change), every endpoint is tried once per operation
> and the last `ApiException` is re-raised — same bounded behavior as an
> all-endpoints-down connectivity failure. No infinite retry.

### 7. `get_pod_exec_stream` (websocket)

This uses `stream(self.v1_api.connect_get_namespaced_pod_exec, ...)` and returns
a long-lived websocket — mid-stream failover isn't meaningful. Wrap only the
**initial connection** with the same rotate-and-retry (build the stream against
the active endpoint; on a connectivity error, rotate once and rebuild). Failures
after the stream is established surface to the caller as today.

## Concurrency summary

- `threading.Lock` (gevent-cooperative) guards: lazy `endpoint.build()` and the
  compare-and-swap in `_rotate_from`. Both are short critical sections.
- Reading `_active_idx` for a call is a plain int read (atomic enough under
  gevent's cooperative scheduling; the CAS in `_rotate_from` is the only
  writer and is locked).
- Node cache (`update_nodes`, 60s TTL) needs **no** invalidation on failover:
  same cluster ⇒ identical node list across endpoints.

## Observability — failover metrics / counters

Ships **now** (not deferred). There is no Prometheus/statsd stack in this app
today, so the counters live **in-process on the controller** and are surfaced
through the existing `get_statistics()` path (already rendered on the dashboard,
`apps/home/routes.py:57`) and structured logs. A `/metrics`-style export is
noted as an easy future add-on, not built here.

### State

Per-endpoint counters plus a small ring of recent events, all mutated under the
same `self._lock`:

```python
self._failover_stats = {
    "active_path":   None,        # kubeconfig currently in use
    "active_since":  0.0,         # epoch when it became active
    "total_failovers": 0,         # rotations since process start
    "per_endpoint": {             # keyed by kubeconfig path
        path: {"failovers": 0, "last_error": None, "last_failover_ts": 0.0}
        for path in app_config.K8S_CONFIGS
    },
    "recent": deque(maxlen=20),   # [{ts, from_path, to_path, status, reason}]
}
```

`_record_failover(ep, exc)` (called by the decorator before `_rotate_from`):

- increments `total_failovers` and `per_endpoint[ep.path]["failovers"]`;
- stores a compact `last_error` (exception class + HTTP status, no bodies);
- appends a `recent` entry with `from`/`to` paths and a `reason`
  (`"timeout" | "conn_refused" | "auth_401" | "auth_403" | "server_5xx"`);
- emits a **`WARNING`** log:
  `k8s failover: <from_path> -> <to_path> reason=<reason> (<exc>)`.

`_set_active(idx)` updates `active_path`/`active_since` and logs an **`INFO`**
line once when an endpoint first becomes active.

### Flapping detection (why ops notice)

"Flapping" = repeated rotations in a short window. Expose a derived **rate**, not
just a lifetime total, so a brief blip is distinguishable from an endpoint that
keeps dropping:

- `failovers_last_5m` — count of `recent` entries within the last 300 s.
- `is_flapping` — `True` when `failovers_last_5m >= K8S_FLAP_THRESHOLD`
  (new env var, default `3`). When it flips to `True` the controller logs a
  single **`ERROR`**: `k8s endpoints flapping: N failovers in last 5m`
  (edge-triggered — logged on the transition, not every rotation, to avoid log
  spam).

### Surface

`get_statistics()` gains a `"kubeconfig"` block (only when
`len(self._endpoints) > 1`, so single-config deployments are unchanged):

```python
"kubeconfig": {
    "active": active_path,
    "active_since": active_since,
    "total_failovers": total_failovers,
    "failovers_last_5m": n,
    "is_flapping": bool,
    "endpoints": [
        {"path": p, "failovers": c, "last_error": e, "active": p == active_path}
        for p, ... in per_endpoint
    ],
}
```

The dashboard stats view renders it; ops see the active kubeconfig, lifetime and
recent failover counts, and a flapping badge. A read-only
`get_failover_stats()` accessor returns the same dict for a future
`/metrics`/health endpoint or alerting scrape.

> **Reset semantics:** counters are process-lifetime (reset on restart) — matches
> the single-worker gunicorn model where the controller singleton lives for the
> worker's lifetime. `recent`/`last_5m` naturally age out via the deque + window.

## Backward compatibility

- Single-path `K8S_CONFIG` → one endpoint → decorator loop length 1 → no
  behavior change.
- `self.v1_api` et al. remain attribute-accessible and return real client
  objects; disabled state still yields `None`.
- kubectl now always passes `--kubeconfig`; with one endpoint this points at the
  same file the ambient config resolved to (set `K8S_CONFIG` explicitly if you
  previously relied on `$KUBECONFIG`/`~/.kube/config` implicitly — documented).

## Testing plan

Update `tests/test_k8s_controller.py` fixture: `client.CoreV1Api` etc. now take
an `api_client` arg → change stubs to `lambda *a, **k: MagicMock()`. The
property-based `self.v1_api` keeps the existing `ctrl.v1_api.method = MagicMock`
assertions valid (single endpoint ⇒ stable object).

New cases:

1. **Failover on timeout:** two endpoints; first `v1_api.list_*` raises
   `urllib3.ReadTimeoutError` → assert the call is retried on endpoint #2 and
   returns its result.
2. **Sticky:** after failing over to #2, a subsequent call uses #2 directly
   (no reset to #0).
3. **Circle / wrap:** with #0 active and failing, rotation goes #0→#1→…→#0.
4. **Failover on auth:** `ApiException(status=401)` **and** `status=403` each
   rotate to the next endpoint and succeed there; assert `reason` is
   `auth_401`/`auth_403` in the recorded stats.
5. **No failover on valid API error:** `ApiException(status=404)` (and `409`,
   `400`) propagate without rotating (`_active_idx` unchanged).
6. **All endpoints down / all-auth-fail:** failover error on every endpoint ⇒
   last exception re-raised after exactly `len(endpoints)` attempts (covers both
   connectivity-everywhere and 401-everywhere — no infinite loop).
7. **Thundering herd:** two "greenlets" both observe a failure on #0 ⇒
   `_active_idx` advances by exactly one (to #1), not two.
8. **kubectl failover:** `TimeoutExpired` and a `CalledProcessError` with an
   `Unauthorized` stderr each rotate and retry with `--kubeconfig` pointing at
   the next path; `CalledProcessError` with a `NotFound` stderr propagates.
9. **create_lab granularity:** connectivity failure on the 3rd
   `create_k8s_resource` is retried on the sibling endpoint and the lab
   completes (the whole `create_lab` is not restarted).
10. **Metrics counters:** each failover increments `total_failovers` and the
    per-endpoint counter, records a `recent` entry, and populates the
    `"kubeconfig"` block in `get_statistics()`; a valid-API-error path leaves all
    counters at zero.
11. **Flapping flag:** `K8S_FLAP_THRESHOLD` failovers within 5 min flips
    `is_flapping` true and logs the edge-triggered `ERROR` exactly once.
12. **Backward compat:** single-path config behaves as today, `get_statistics()`
    has **no** `"kubeconfig"` block, disabled state (`v1_api is None`) preserved.

## Files touched

- `apps/config.py` — `K8S_CONFIGS` parsing (+ `K8S_CONFIG` alias),
  `K8S_FLAP_THRESHOLD` (default `3`).
- `apps/controllers/kubernetes.py` — `_KubeEndpoint`, `with_failover`,
  `_is_failover_error`, `_kubectl_stderr_is_failover`, `_run_kubectl`,
  `_rotate_from`, `_active_endpoint`, `_set_active`, `_record_failover`,
  `get_failover_stats`, properties for the four clients; decorate
  read/write/kubectl/exec methods; extend `get_statistics()` with the
  `"kubeconfig"` block.
- `env-template` + `doc/INSTALL.md` — document comma-separated `KUBECONFIG` and
  `K8S_FLAP_THRESHOLD`.
- `tests/test_k8s_controller.py` (+ `tests/test_k8s_tape.py` if it constructs
  clients) — fixture tweak + new failover & metrics cases.
- (optional) dashboard stats template — render the `"kubeconfig"` block /
  flapping badge.

## Open questions / follow-ups

1. **Per-endpoint `request_timeout`?** Today one global `K8S_REQUEST_TIMEOUT`.
   Fine to keep global; note if any endpoint is known-slower.
2. **`/metrics` (Prometheus) export** — `get_failover_stats()` is shaped to be
   scrape-friendly; wiring an actual endpoint/exporter is a later add-on, not in
   this change.
3. **Alerting on `is_flapping`** — the `ERROR` log is the current signal; hook it
   into whatever log-based alerting ops already run.
