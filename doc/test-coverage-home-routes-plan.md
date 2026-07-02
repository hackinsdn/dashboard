# Implementation plan: raise unit-test coverage of `apps/home/routes.py`

## Context

`apps/home/routes.py` has ~28 routes. The existing suites cover only the CRUD
routes for users, groups, labs, and lab-categories. Eighteen routes and seven
models (`LabInstances`, `LabAnswers`, `LabAnswerSheet`, `LabMetadata`,
`UserFeedbacks`, `UserLikes`, `HomeLogging`) have no direct coverage. This plan
adds four new pytest files that follow the exact same approach as
`tests/test_lab_categories.py` / `tests/test_labs.py`: session-scoped temp
SQLite DB, the `clabernetes` import stub, ordered/stateful test classes, and
`login`/`logout` helpers.

Reuse verbatim (per file) the harness boilerplate from
`tests/test_labs.py` lines 29-128: temp `DATA_DIR`, the `apps.controllers.clabernetes`
sys.modules stub, `flask_app` import + `TESTING`/`WTF_CSRF_ENABLED`, the
`_cleanup_temp_data_dir`/`app`/`client` fixtures (app fixture does
`create_all()` only, no `drop_all`), and `login`/`logout`.

### Cross-file isolation (critical)

All test modules share ONE singleton app/DB (`apps/config.py` reads `DATA_DIR`
once at first import). So every new file must use a **unique username prefix**
and unique group/lab/category names, exactly like the existing files
(`us`/`gr`/`lb`). Proposed prefixes: `li` (instances), `la` (answers),
`up` (uploads), `mp` (misc pages).

### k8s handling

Routes reference the imported module object as `k8s` (`from apps.controllers
import k8s, c9s`). Stub only the happy paths by monkeypatching that object,
e.g. `monkeypatch.setattr("apps.home.routes.k8s.create_lab", lambda *a, **k: (True, []))`.
Permission/not-found/validation branches return *before* any k8s call and need
no stub. Use the function-scoped `monkeypatch` fixture inside the specific test
method (works fine inside an ordered class).

Model seeding notes (from `apps/home/models.py`):
- `LabInstances(user_id=, lab_id=, is_deleted=, expiration_ts=)`; set
  `inst.k8s_resources = []` (setter JSON-encodes) so status templates render.
- `LabAnswers(user_id=, lab_id=)` then `.answers = json.dumps({...})`,
  `.grades = json.dumps({...})`.
- `LabAnswerSheet()` then `.lab_id = ...; .set_answers({q: a})`.
- `LabMetadata(lab=lab, is_clab=False)` then `lab_md.md = {"uploads": [...]}`.
- `UserFeedbacks(user_id=, stars=, comment=, is_hidden=)`.
- `UserLikes(user_id=)`.
- Labs seeded directly are fine here (these routes don't render
  `extended_desc_str`), but for `view_labs`-style rendering create via route.

---

## File 1: `tests/test_lab_instances.py`  (prefix `li`)

Seed: admin, teacher, student, labcreator; a Lab; a group the student owns/
assists (for the privileged-group visibility branch); LabInstances rows.

`running_labs` (`/running/`):
- student login, GET `/running/` -> 200; only their own non-deleted instances.
- `filter_group=<gid>` with a group the user can't see / bad id -> "Group not found"
  when group missing/deleted; admin sees SYSTEM-excluded groups list.
- privileged (owner/assistant) user sees instances of labs allowed to their group.

`run_lab` (`/run_lab/<id>`):
- GET missing lab -> "Lab not found".
- GET valid lab -> 200 renders run form; admin sees the extra "Never expires" option.
- GET when an instance already runs -> 302 redirect to `view_lab_instance`.
- POST invalid `lab_expiration` -> "Invalid lab duration/expiration".
- POST valid, `k8s.create_lab` monkeypatched to `(True, [])` -> renders
  run_lab_status; a `LabInstances` row + `HomeLogging` create_lab row exist.
- POST valid, `k8s.create_lab` -> `(False, "boom")` -> "Error Running Labs" +
  a `HomeLogging` success=False row.

`check_lab_status` (`/lab_status/<id>`):
- missing -> "Lab not found"; other user's instance -> "not authorized";
  owner -> 200 (seed instance with `k8s_resources = []`).

`xterm` (`/xterm/<id>/<kind>/<pod>/<container>`):
- missing -> "Lab not found"; student/labcreator on another's instance ->
  "not authorized"; owner -> 200 with host string in body.

`view_lab_instance` (`/lab_instance/view/<id>`):
- missing / deleted instance -> error; instance of unknown lab -> error;
  non-owner non-admin without privileged group -> "Not authorized".
- owner happy path: monkeypatch `k8s.get_lab_resources` -> `[]` -> 200 renders view.

`cancel_restart_lab_instance` (`/lab_instance/cancel/<id>`):
- missing/deleted -> error; non-owner non-admin -> "Not authorized".
- owner: monkeypatch `k8s.delete_resources_by_name` -> `[1]* len` -> 302 redirect
  to run_lab; instance `is_deleted` True.

`view_finished_labs` (`/finished_labs`):
- student sees only their own `is_deleted=True` instances; bad `filter_group`
  -> "Group not found"; admin/group-filter visibility branch.

---

## File 2: `tests/test_lab_answers.py`  (prefix `la`)

Covers the `LabAnswers` / `LabAnswerSheet` models via `list_lab_answers` and
`add_answer_sheet`. Seed: admin, teacher (owner of a group), student (member);
a Lab allowed to that group; `LabAnswers` rows for the student; a
`LabAnswerSheet`.

`list_lab_answers` (`/lab_answers/list`):
- student rejected (decorator admin/teacher) -> "Unauthorized request".
- admin GET -> 200; lists the seeded answer with a computed `score`.
- `filter_lab=<unknown>` -> "Invalid Lab provided for filtering."
- `filter_group=<not-visible>` -> "Invalid Group provided for filtering."
- `check_answer_sheet=1` without `filter_lab` -> "must provide a Lab".
- `check_answer_sheet=1` + `filter_lab` with no sheet -> "No Lab Answer Sheet".
- scoring: seed a sheet answer + a matching student answer, assert score "100.00";
  a grade value (numeric) is honored over regex match.
- teacher sees only answers for labs in their privileged groups (non-admin branch).

`add_answer_sheet` (`/lab_answers/answer_sheet/`):
- GET no `lab_id` -> 200 lab picker.
- GET `lab_id` not in labs -> "Invalid Lab provided."
- GET valid `lab_id` -> 200, pre-fills existing answers.
- POST `question`/`answer` lists -> creates a new `LabAnswerSheet`, "saved!";
  verify `sheet.answers_dict`.
- POST again editing -> updates the same sheet (no duplicate row).

---

## File 3: `tests/test_lab_uploads.py`  (prefix `up`)

Covers `LabMetadata` + the upload endpoints. Seed: admin, teacher, labcreator
(as `updated_by` on an owned Lab), a second labcreator (non-owner); two Labs
(one owned by the labcreator). Uses the real temp `UPLOAD_DIR` (under the temp
`DATA_DIR`).

`upload_lab_file` (POST `/labs/upload-file`, multipart):
- student rejected by decorator.
- no `file` part -> 400 "No file part".
- empty filename -> 400 "No file selected".
- disallowed extension (e.g. `.exe`) -> 400 "File extension not allowed".
- oversize (monkeypatch `LAB_UPLOAD_MAX_SIZE` low, or post >limit bytes) -> 400.
- valid `.txt` upload, no `lab_id` -> 200 JSON with `saved_filename`; file exists
  on disk in `UPLOAD_DIR`.
- valid upload with `lab_id=<owned lab>` -> metadata persisted:
  `lab.lab_metadata.md["uploads"]` contains the entry.

`serve_upload` (GET `/uploads/<filename>`):
- after an upload, GET the returned URL -> 200 and body matches file contents;
  unknown filename -> 404.

`get_lab_uploads` (GET `/labs/<id>/uploads`):
- missing lab -> 404; labcreator on another's lab -> 403 "Unauthorized";
  owner/admin -> 200 with the uploads list.

`delete_lab_upload` (DELETE `/labs/<id>/uploads/<filename>`):
- missing lab -> 404; labcreator non-owner -> 403; lab without metadata -> 404
  "No uploads found"; filename not in list -> 404; valid -> 200 `{"status":"ok"}`,
  entry removed from `lab.lab_metadata.md["uploads"]`. The handler also
  `os.remove`s the file but tolerates a missing one (FileNotFoundError is
  logged, not fatal), so the happy-path test needn't put a real file on disk.

---

## File 4: `tests/test_misc_pages.py`  (prefix `mp`)

Covers the remaining simple/feedback routes and `UserFeedbacks`/`UserLikes`.
Seed: admin, teacher, student; a couple of `UserFeedbacks` (one hidden).

Static GETs (login-required only):
- `/gallery`, `/documentation`, `/contact`, `/finished-lab-infos/<id>` -> 200
  for any logged-in user; anonymous -> redirect to `/login/`.

`index` (`/index`):
- logged-in GET -> 200 (k8s.get_statistics failure is swallowed; assert it
  renders). Seed a `UserLikes` for the user and assert `has_liked` path works.
- feedback modal: with no `UserFeedbacks` for the user and an old
  `created_at`, `show_feedback_modal` becomes True (assert via rendered marker
  or by seeding/reading cache); with an existing feedback it stays False.

`feedback_view` (`/feedback_view`):
- admin sees hidden + visible feedbacks; non-admin sees only non-hidden
  (assert the hidden comment string is absent for the student).

`hide_feedback` (POST `/feedback/hide`, admin-only):
- non-admin -> "Unauthorized request".
- admin missing `feedback_id` or bad `action` -> 302 redirect, no change.
- admin `action=hide` -> feedback `is_hidden` True; `action=unhide` -> False.

---

## Files to create
- `tests/test_lab_instances.py`
- `tests/test_lab_answers.py`
- `tests/test_lab_uploads.py`
- `tests/test_misc_pages.py`

No production changes expected. If any handler surfaces a real bug (as the
group-delete `dict_keys` issue did earlier), stop and confirm the fix before
asserting a happy path.

## Verification
```
pip install -r requirements-dev.txt
pytest tests/test_lab_instances.py tests/test_lab_answers.py \
       tests/test_lab_uploads.py tests/test_misc_pages.py -v
pytest tests/ -v          # whole suite must stay green (unique prefixes prevent
                          # cross-file username/name collisions in the shared DB)
```
Optionally measure the delta with `pytest --cov=apps.home.routes tests/`
(requires `pytest-cov`, not currently in requirements-dev.txt).
