# Implementation plan: unit tests for `apps/cli/lab_schedule.py`

## Context

`apps/cli/lab_schedule.py` holds the two scheduled-job helpers (invoked from
`apps/cli/routes.py` as CLI commands) with no coverage:

- `alert_expiring_labs(app, send_email=False)` - finds non-deleted lab
  instances expiring within `LAB_EXPIRATION_WARN_SEC` (48h) and, when
  `send_email` is set, emails each owner that has an address.
- `run_delete_expired_labs(app)` - finds instances already past
  `LAB_EXPIRATION_TOLERANACE_SEC` (48h) beyond expiry, deletes their k8s
  resources and soft-deletes them.

Both take the Flask `app`, query models via the global session (so tests push
an app context and pass `flask_app`), and lean on external systems that are
easily mocked: `Mail`/`Message` for e-mail and `k8s.delete_resources_by_name`
for teardown. The mail template `mail/lab_expiration_alert.html` only references
`config.BASE_URL`/`user`/`lab`/`end_time` (no `url_for`/`current_user`/request),
so `render_template` works inside a plain app context.

### Shared-DB isolation (important)

Every test module shares one singleton SQLite DB (config reads `DATA_DIR` once),
and these two functions scan **all** `LabInstances` in the DB - so instances
seeded by other modules (e.g. `test_api_engagement` sets `expiration_ts` via the
extend test) can leak into the result set. The plan handles this two ways:

1. In session setup, **neutralise pre-existing instances** once
   (`LabInstances.query ... update(expiration_ts=None)` / or set them deleted)
   so only this file's rows can match; and
2. a **function-scoped cleanup** that deletes this file's own instances (by the
   `ls`-prefixed user ids) before each test, so each test controls its exact set
   and can assert precise outbox counts.

Assertions also stay targeted (this file's user emails / this file's instance
flags) rather than relying on global state.

## New file: `tests/test_lab_schedule.py`  (prefix `ls`)

Standard harness (clabernetes stub + `from run import app`), wrapped in
`flask_app.app_context()`. Import the two functions from
`apps.cli.lab_schedule`. Seed: `ls_user` (with email), `ls_noemail` (email ""),
and one `ls_lab`. Set `flask_app.config["MAIL_DEFAULT_SENDER"]`.

Mocks:
- `mail_outbox` fixture: `monkeypatch.setattr("apps.cli.lab_schedule.Mail", FakeMail)`
  where `FakeMail(app).send(msg)` appends `msg` to a list (recipients readable
  via `msg.recipients`). `Message` stays real.
- `fake_k8s` fixture: `monkeypatch.setattr("apps.cli.lab_schedule.k8s", ns)` with
  `delete_resources_by_name` returning `[]` (or raising, for the error test).
- helper `make_instance(user_id, expiration_ts, is_deleted=False, lab_id=ls_lab)`
  and `WARN`/`TOL` = 48h constants derived from `utcnow()`.

### TestAlertExpiringLabs
- **send_email=False** with an expiring instance for `ls_user` -> nothing sent
  (`ls_user`'s email not in the outbox); the "No e-mail to send" branch runs.
- **send_email=True**, expiring instance for `ls_user` -> exactly one message to
  `ls_user@...`.
- **send_email=True**, expiring instance for `ls_noemail` (no address) -> no send
  (the `if user.email` guard skips it), no crash.
- **send_email=True**, expiring instance whose `user_id` is a missing user id ->
  the `if not user` branch continues; nothing sent, no crash.
- **not-expiring**: an instance expiring well beyond the 48h window -> not
  alerted (not in outbox).
- **excluded rows**: an `expiration_ts=None` instance and an `is_deleted=True`
  instance are never selected.

### TestRunDeleteExpiredLabs
- **expired instance**: `expiration_ts` older than `now - TOL`; `k8s`
  faked -> the instance ends `is_deleted=True` with `finish_reason="Lab Expired"`,
  and `delete_resources_by_name` was called with its resources.
- **within tolerance**: `expiration_ts` recent (not yet past tolerance) -> left
  untouched (`is_deleted` stays False).
- **teardown error**: `k8s.delete_resources_by_name` raises -> the exception is
  swallowed/logged and the instance is **not** soft-deleted (`is_deleted` stays
  False).

## Note (not a required fix)

In `alert_expiring_labs`, the send path builds the body with `lab.title`; if an
instance references a lab id that no longer exists, `lab` is `None` and this
raises. Tests always use a valid `ls_lab`, so this edge isn't exercised. Worth a
follow-up guard, but out of scope here unless you want it fixed.

## Files to create
- `tests/test_lab_schedule.py`

No production changes expected.

## Verification
```
pip install -r requirements-dev.txt
pytest tests/test_lab_schedule.py -v
pytest tests/ -v          # whole suite stays green
pytest tests/ --cov=apps.cli.lab_schedule --cov-report=term-missing
```
