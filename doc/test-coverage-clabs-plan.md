# Implementation plan: raise unit-test coverage of `apps/clabs/routes.py`

## Context

`apps/clabs/routes.py` is a single blueprint with one substantial route:
`upsert` (GET/POST `/clabs/upsert/` and `/clabs/upsert/<clab_id>`) - the
create/edit form for **ContainerLabs** (`Labs` rows with `is_clab=True`, backed
by a `LabMetadata` record). It has no test coverage. This plan adds one pytest
file, `tests/test_clabs.py`, following the established approach (session-scoped
temp SQLite DB, `clabernetes` import stub, ordered/stateful classes, unique
username prefix `cl`, `login`/`logout` helpers).

### How `upsert` breaks down (for test design)

Most branches never touch Kubernetes/clabernetes and are trivially testable:
load/permission gating, category handling, and the **manifest-only** save. The
`c9s` (clabernetes) and `k8s` controllers are only reached when files are
uploaded or registry secrets are edited - and both are lazy proxies
(`c9s = _LazyProxy(lambda: C9sController())`), currently pointed at the
`_StubC9sController` that raises on any attribute. So:

- **No-c9s/no-k8s paths** (gating, validation, manifest-only create/edit): no
  mocking needed - the stub is never called.
- **Secret editing**: replace the module-level name
  `apps.clabs.routes.k8s` with a small fake exposing `create_registry_secret`
  / `delete_secret_by_name` (setattr on the string path). For a brand-new clab
  `md` has no `topology`, so the `check_topology_secrets` branch is skipped and
  this path saves without `c9s`.
- **File upload / topology conversion**: replace `apps.clabs.routes.c9s` with a
  fake exposing `process_clab_topology` / `convert_clab` /
  `check_topology_secrets`. Files are written under the temp `CLABS_DIR`.

Confirmed helpers/config: `apps.utils.list_files` returns an empty set for a
non-existent dir (safe on new clabs); `CLABS_DIR` is under the temp `DATA_DIR`;
`CLABS_UPLOAD_MAX_FILES` defaults to 200; the template renders `clab.extended_desc_str|default('',true)`
(Jinja swallows the property's `AttributeError` on an empty clab, so GET-new
renders).

## Production fix (required for the "too many files" test)

`apps/clabs/routes.py:137` builds the error string with an **undefined
variable** `files_count`:
```python
"result": f"Too many files: {files_count} (max {max_files})"
```
`files_count` is never assigned, so the moment more than `CLABS_UPLOAD_MAX_FILES`
files are posted this raises `NameError` (HTTP 500) instead of the intended
400. Fix: use `len(files)` (the variable already in scope):
```python
"result": f"Too many files: {len(files)} (max {max_files})"
```
The too-many-files test depends on this fix; the rest of the suite does not.

## New file: `tests/test_clabs.py`  (prefix `cl`)

Seed: `cladmin` (admin), `clcreator`/`clcreator2` (labcreator), `clstudent`
(student); one generic `LabCategories` `clcat` (NOT named "ContainerLab", so the
category-required branch is reachable); one plain non-clab `Labs` (no metadata);
one existing clab (`Labs` + `LabMetadata(is_clab=True)`, `updated_by=cladmin`).
A `clab_form(**overrides)` helper builds the full POST body
(`clab_title, clab_desc, clab_extended_desc, clab_guide, clab_yaml, clab_goals`,
plus `clab_categories`/`clab_allowed_groups` lists).

### TestRoleGating
- student GET `/clabs/upsert/new` -> "Unauthorized request".

### TestLoadAndPermissions
- unknown `clab_id` -> "ContainerLab not found".
- a non-clab lab id -> 302 redirect to `home_blueprint.edit_lab` (`/labs/edit/<id>`).
- labcreator opening another user's clab (`updated_by != self`) -> "Unauthorized access".
- **no categories exist**: temporarily flip every active `LabCategories` to
  `is_deleted=True` (other modules share this DB), GET new -> "No Lab Categories
  found", then restore in a `finally` (same technique as
  `tests/test_labs.py::TestNoCategoriesGuard`).

### TestGetForm
- admin GET `/clabs/upsert/new` -> 200 (form renders with `clcat`).

### TestCreate (manifest-only; no c9s/k8s)
- POST with a category but no manifest/giturl/files -> 400 "Provide GIT URL or files".
- POST with a manifest but no category selected (and no "ContainerLab" category
  present) -> "Please select at least one category".
- POST valid (manifest + `clab_categories=[clcat]`) -> 201 "ContainerLab saved
  with success"; the created `Labs` row has `is_clab` True, `manifest` set, and a
  `HomeLogging(action="upsert_clab", success=True)` row exists. Capture the id.

### TestEditExisting (reuses the id created above)
- GET the created clab -> 200.
- POST editing the title -> 201; verify the DB change.

### TestSecrets (k8s replaced with a fake)
- `monkeypatch.setattr("apps.clabs.routes.k8s", fake_k8s)` where
  `create_registry_secret -> (True, "")` and `delete_secret_by_name -> None`.
- POST create with `secret-name/secret-server/secret-user/secret-pass` + a
  category + manifest -> 201; the clab's `lab_metadata.md["secrets"]["name"]`
  contains the new secret name.

### TestFileUpload (c9s replaced with a fake)
- `monkeypatch.setattr("apps.clabs.routes.c9s", fake_c9s)` where
  `process_clab_topology -> (True, {"ports": {}, "nodes": {}})` and
  `convert_clab -> (True, "converted-manifest")`.
- POST create with `clab_files=(BytesIO(b"..."), "topo.yaml")` +
  `relative_paths="topo.yaml"` + a category -> 201; `clab.manifest` becomes
  "converted-manifest".
- `process_clab_topology -> (False, "bad topo")` -> 400 (and the temp dir is
  cleaned up).
- `process -> (True, {...})`, `convert_clab -> (False, "convert err")` -> 400.

### TestTooManyFiles (depends on the production fix above)
- `monkeypatch.setitem(flask_app.config, "CLABS_UPLOAD_MAX_FILES", 1)`, POST two
  files -> 400 "Too many files".

### TestAutoAppendContainerLabCategory (last - it seeds a "ContainerLab" category)
- seed a `LabCategories(category="ContainerLab")`; POST new with a manifest and
  **no** `clab_categories` selected -> 201; the saved clab has the "ContainerLab"
  category auto-attached. (Runs last so it doesn't disturb the category-required
  test.)

## Notes
- Unique `cl` username prefix + unique category/lab names to avoid collisions in
  the shared singleton DB; `app` fixture does `create_all()` only (no `drop_all`).
- Fakes for `c9s`/`k8s` are plain objects/`types.SimpleNamespace` assigned over
  the whole module-level name (they're lazy proxies, so patching individual
  attributes is less reliable than replacing the reference).
- Files/dirs land under the temp `CLABS_DIR`; nothing touches real infra.

## Files to create / modify
- **Fix**: `apps/clabs/routes.py:137` (`files_count` -> `len(files)`).
- **New**: `tests/test_clabs.py`.

## Verification
```
pip install -r requirements-dev.txt
pytest tests/test_clabs.py -v
pytest tests/ -v          # whole suite stays green
pytest tests/ --cov=apps.clabs.routes --cov-report=term-missing
```
