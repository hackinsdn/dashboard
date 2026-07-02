# Implementation plan: unit tests for `apps/controllers/clabernetes.py`

## Context

`apps/controllers/clabernetes.py` defines `C9sController`, the pure-logic helper
that processes ContainerLab topologies and drives the external `clabverter`
binary. It has **no coverage** - and unlike everything tested so far, every
existing suite *stubs it out* because the module raises at **import time** if the
`clabverter` binary is missing:

```python
CLABVERTER_BIN = "/usr/local/bin/clabverter"
if not os.path.exists(CLABVERTER_BIN) or not os.access(CLABVERTER_BIN, os.X_OK):
    raise ValueError("Missing executable 'clabverter'. ...")
```

The good news: the module imports only stdlib + `yaml` (no `apps.*`), and only
`convert_clab` actually shells out to the binary (via `subprocess.run`, trivially
mockable). Every other method is pure logic. So this is a **plain unit-test
file** - no Flask app, no DB, no login/client harness - which makes it simpler
than the route suites, just structured the same way (one ordered pytest file).

### The one trick: import past the binary guard

Load the module **directly from its file** with `importlib`, temporarily
patching `os.path.exists`/`os.access` so the guard passes. Loading by path (not
`from apps.controllers import ...`) sidesteps `apps/controllers/__init__.py`,
which eagerly imports the real `K8sController`/`GitController` (kube config, etc.)
and would defeat the isolation the other suites rely on:

```python
import importlib.util, os
_PATH = os.path.join(REPO_ROOT, "apps", "controllers", "clabernetes.py")
_real_exists, _real_access = os.path.exists, os.access
os.path.exists = lambda p, *a: True if p == "/usr/local/bin/clabverter" else _real_exists(p, *a)
os.access = lambda p, m, *a, **k: True if p == "/usr/local/bin/clabverter" else _real_access(p, m, *a, **k)
try:
    spec = importlib.util.spec_from_file_location("clabernetes_under_test", _PATH)
    c9s_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c9s_mod)
finally:
    os.path.exists, os.access = _real_exists, _real_access
C9sController = c9s_mod.C9sController
```

This creates a standalone module (`clabernetes_under_test`) that never touches
`sys.modules["apps.controllers.clabernetes"]`, so it can't interfere with the
other files' stub, and needs nothing from the app. `subprocess` is mocked via
`monkeypatch.setattr(c9s_mod.subprocess, "run", ...)`.

## New file: `tests/test_clabernetes.py`

A `c9s` fixture returns `C9sController()`. Uses pytest's built-in `tmp_path` for
the filesystem-touching topology tests. No temp DB / client.

### TestFilenameFromUploads (`filename_from_uploads`)
- a path ending in `/<uploaded>` resolves to the bare uploaded name
  (`"configs/r1.cfg"`, `["r1.cfg"]` -> `"r1.cfg"`).
- no match -> the original string is returned unchanged.

### TestCleanUpForClabGraph (`clean_up_for_clab_graph`)
- a topology with `binds` under `defaults`, a `kind`, a `group` and a `node`
  comes back with every `binds` key removed.
- a minimal topology (only `nodes`, no defaults/kinds/groups) is handled without
  KeyError.

### TestCheckTopologySecrets (`check_topology_secrets`)
- a known `imagePullSecrets` name on a node is rewritten to its mapped k8s name
  and no failure is reported.
- an unknown secret is left and produces a descriptive failure entry.
- the same rewrite/failure logic is covered for `defaults` and `kinds` blocks.

### TestProcessClabTopology (`process_clab_topology`, uses `tmp_path`)
- **no `*.clab.y*ml` file** in the dir -> `(False, "No ContainerLab topology file...")`.
- **two** clab files -> `(False, "Too many ContainerLab topology files...")`.
- a file that is invalid YAML / missing `topology` / has zero nodes ->
  `(False, "Failed to load ContainerLab topology...")`.
- **happy path**: write a valid `test.clab.yaml` (a node with `ports`
  `["8080:80", "53/udp"]`, a `startup-config`, a `binds` entry, and
  `imagePullSecrets: ["myreg"]`) plus an uploaded `r1.cfg`; call with
  `clab_uuid="abc"`, `secrets={"myreg": "clab-secret-x"}`. Assert:
  - returns `(True, topology)`;
  - `topology["name"] == "clab-abc"`;
  - `topology["ports"] == {"<node>": {"80": "8080:80", "53": "53/udp"}}`;
  - the node's `imagePullSecrets` became `["clab-secret-x"]`;
  - `binds` was stripped from the returned topology (clean-up step);
  - the original file was unlinked and `clab-abc.clab.yaml` now exists on disk.
- **failed secrets**: same topology but `secrets={}` ->
  `(False, "Failed to find imagePullSecrets...")`.

### TestConvertClab (`convert_clab`, `subprocess.run` mocked)
- success: mock `run` to return an object whose `stdout` is a multi-doc YAML
  string containing a `kind: Namespace` doc among others -> `(True, manifest)`
  where the Namespace doc has been removed and the rest re-joined with `---\n`.
- no Namespace doc in the output -> `(False, "could not find Namespace component")`.
- `subprocess.CalledProcessError` (with `.stderr`) -> `(False, "Convert ContainerLab failed: ...")`.
- any other exception from `run` -> `(False, "Convert ContainerLab failed: ...")`.

### TestVisualizerManifest (`get_topology_visualizer_manifest`)
- `%CLAB_UUID%` is substituted throughout; the result contains the Deployment,
  Service and a ConfigMap whose `topology-data.yaml` embeds the dumped topology;
  the three docs are joined with `---\n`.

## Files to create
- `tests/test_clabernetes.py`

No production changes expected. If a method turns out to misbehave on a valid
input (e.g. the `PORT_RE` edge cases in the docstring), stop and confirm before
asserting around it.

## Verification
```
pip install -r requirements-dev.txt
pytest tests/test_clabernetes.py -v
pytest tests/ -v          # whole suite stays green (this file is self-contained
                          # and does not disturb the shared clabernetes stub)
pytest tests/ --cov=apps.controllers.clabernetes --cov-report=term-missing
```
