# Implementation plan: unit tests for git.py, kubernetes.py, k8s/routes.py, utils.py

## Context

Four low-coverage modules, tackled with four new test files following the
established conventions (harness boilerplate, ordered classes, `login`/`logout`
where a client is needed, monkeypatch for external systems):

| Module | New file | Harness needed? |
|---|---|---|
| `apps/controllers/git.py` | `tests/test_git_controller.py` | app (for `current_app`) |
| `apps/controllers/kubernetes.py` | `tests/test_k8s_controller.py` | app (for `current_app`/`app_config`) |
| `apps/k8s/routes.py` | `tests/test_k8s_routes.py` | full (app + DB + login) |
| `apps/utils.py` | `tests/test_utils.py` | full (app + DB) |

All four import cleanly under the standard harness (install the `clabernetes`
sys.modules stub, then `from run import app`); none needs the direct-file-load
trick used for `clabernetes` itself. External systems are mocked: GitPython for
`git.py`, the kubernetes client for `kubernetes.py`, and the controller methods
behind `apps.k8s.routes.k8s` for the routes.

## Production fixes (recommended)

`apps/utils.py:259` `remove_empty_folders` is **dead code with a `NameError`**:
```python
folders = list(os.walk(mydir))[1:]   # 'mydir' is undefined; the param is 'folder'
```
It is called nowhere in the codebase. Recommendation: fix `mydir` -> `folder`
(one line) so it can be covered, or delete the function outright. Default in this
plan: **fix + test** (consistent with the earlier `files_count`/`delete_group`
fixes). Flagging for a decision.

(Note: `format_duration` annotates a local as `output: List[str]` without
importing `List`, but local annotations are not evaluated at runtime, so it is
harmless - no fix needed.)

## File 1: `tests/test_git_controller.py`

Import `GitController` from `apps.controllers.git` (after the stub + app import).
Mock the `git` module the controller uses via `monkeypatch.setattr` on
`apps.controllers.git.git.*`. Runs inside `flask_app.app_context()` so
`current_app.logger` works in the error branches; `tmp_path` for the filesystem.

`update_repo`:
- refresh-interval short-circuit: two quick calls with a large `refresh_interval`
  -> the second returns True without invoking git (assert the mocked clone/pull
  was called at most once).
- clone path (target dir absent): `git.Repo.clone_from` mocked -> True, and it
  was called.
- pull path (target dir present): `git.Repo` mocked so `repo.remotes.origin.pull`
  is a mock -> True, pull called.
- `git.GitCommandError` raised -> False.
- generic `Exception` raised -> False.

`list_files`: create files under `tmp_path`, assert the glob pattern returns them.

`get_file`:
- missing file -> `(False, "Template not found")`.
- existing file -> `(True, <contents>)`.
- read error (e.g. monkeypatch `Path.read_text` to raise) -> `(False, "Failed to read template file...")`.

## File 2: `tests/test_k8s_controller.py`

A `k8s_ctrl` fixture builds a fully-initialised controller without a real
cluster: set `app_config.K8S_NAMESPACE` to a dummy, and monkeypatch
`apps.controllers.kubernetes.config.load_kube_config` plus `client.CoreV1Api` /
`AppsV1Api` / `ApiClient` to return `MagicMock`s, then instantiate
`K8sController()` (so `self.identifiers`, `self.nodes`, `self.v1_api`, etc. are
all wired). Individual tests set return values / side effects on
`ctrl.v1_api` / `ctrl.apps_v1_api`.

Pure logic (no client):
- `try_get_app`: `""` -> `"http://"`; `"https-web"` -> `"https://"`; unknown -> `"http://"`.
- `humanbytes`: bytes/KB/MB/GB boundaries render the expected suffixes.
- `validate_token` -> always True; `get_pods_by_lab_id` -> `[]`.
- `get_pod_hash`: honours an explicit `pod_hash`, else returns a 14-char hex.
- `get_allowed_nodes` / `choose_one_node`: `dry_run` and explicit `allowed_nodes`
  branches (deterministic, no cluster).
- `get_allowed_nodes_str`: comma-joins the above.
- `get_identifier_func`: known key, regex-matched key, and unknown -> None.
- `substitute_identifiers`: a manifest with `${pod_hash}` (dry-run) substitutes;
  an unknown `${bogus}` placeholder -> `(False, "Invalid placeholder bogus")`.

Client-backed (v1_api / apps_v1_api mocked):
- `create_registry_secret`: success -> `(True, ...)`; `create_namespaced_secret`
  raising -> `(False, "Failed to create secret...")`.
- `get_pod_by_name` / `get_service_by_name` / `get_deployment_by_name` /
  `get_config_map_by_name`: mock the read call to return an object with a
  `to_dict()` and status; assert the `is_ok` flag logic (e.g. pod phase
  "Running" -> True).
- `delete_pod_by_name` (+ service/deployment/config_map/secret): success -> True;
  the delete call raising -> False. `delete_secret_by_name` accepts both a dict
  and a bare string.
- `get_resources_by_name` / `delete_resources_by_name`: dispatch by `kind`
  (Pod/Service/Deployment/ConfigMap/Secret/other) hits the right helper.
- `update_nodes` + `get_nodes` + `get_node_ip`: mock `v1_api.list_node()` to
  return fake nodes (with `status.conditions`/`addresses`); assert ready-node
  filtering (respecting `k8s_avoid_nodes`), the `get_nodes` dict shape, and
  `get_node_ip` returning the InternalIP (or None for an unknown node). Reset
  `ctrl.nodes_last_updated = 0` so the 60s cache doesn't skip the refresh.

(The heavy `create_lab` / `get_lab_resources` / `get_statistics` / `list_pods`
paths are out of scope - they orchestrate many client calls and are better left
for a later pass; this file targets the pure + thinly-wrapped methods.)

## File 3: `tests/test_k8s_routes.py`

Full harness + `login` helper. The k8s blueprint is a core (non-optional)
blueprint at prefix `/k8s`. Seed an admin and a non-admin. Monkeypatch
`apps.k8s.routes.k8s.list_pods` / `list_deployments` / `list_services`.

For each of `/k8s/pods`, `/k8s/deployments`, `/k8s/services`:
- non-admin (e.g. teacher) -> "Unauthorized request".
- admin, controller returns a small list -> 200 and the page renders.
- admin, controller raises -> still 200 (the handler swallows the error and
  renders with an empty list).

## File 4: `tests/test_utils.py`

Full harness (DB) inside `flask_app.app_context()`.

Pure functions (no DB):
- `utcnow`: timezone-aware, ~now.
- `parse_lab_expiration`: `"0"` -> None; `"4"` -> an int ~4h in the future.
- `datetime_from_ts`: a valid ts -> ISO string; a bad input -> None.
- `secure_filename`: the docstring cases (`"My cool movie.mov"` ->
  `"My_cool_movie.mov"`, `"../../../etc/passwd"` -> `"etc/passwd"`, umlauts, etc.).
- `format_duration`: `timedelta(0)` -> `"0s"`; negative -> `"--"`; a
  minutes+seconds delta; a multi-day delta (>6 days truncates).
- `list_files`: files under `tmp_path`, with and without `ignore_prefix`.
- `remove_empty_folders` (after the fix): removes an empty subdir, leaves a
  non-empty one.

DB-backed functions (seed rows, prefix `ut`):
- `check_pre_approved`: a non-`user` account -> returns None, unchanged; a
  `user` in a non-SYSTEM group -> promoted to `student` (returns True); a `user`
  whose email is in a group's `approved_users` -> promoted and added to the
  group; a `user` only in a SYSTEM group -> not promoted by that group.
- `update_running_labs_stats`: with seeded non-deleted `LabInstances`, sets
  `g.running_labs` and populates the `running_labs-<id>` cache key
  (`cache.delete` first to avoid cross-module leakage).
- `update_category_stats`: seed categories + a lab attached to one -> the
  returned dict's `category_names`/`usage_counts` reflect the seeded data.
- `update_stats_lab_instances_answers`: seed a couple of finished
  `LabInstances` and `LabAnswers` -> `months`/`labs`/`answers` totals are shaped
  correctly (6 months, totals add up).

## Files to create / modify
- **Fix**: `apps/utils.py:259` (`mydir` -> `folder`) — or drop the dead function.
- **New**: `tests/test_git_controller.py`, `tests/test_k8s_controller.py`,
  `tests/test_k8s_routes.py`, `tests/test_utils.py`.

## Verification
```
pip install -r requirements-dev.txt
pytest tests/test_git_controller.py tests/test_k8s_controller.py \
       tests/test_k8s_routes.py tests/test_utils.py -v
pytest tests/ -v      # whole suite stays green
pytest tests/ --cov=apps.controllers.git --cov=apps.controllers.kubernetes \
       --cov=apps.k8s.routes --cov=apps.utils --cov-report=term-missing
```
