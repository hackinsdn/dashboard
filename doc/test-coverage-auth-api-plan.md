# Implementation plan: raise unit-test coverage of `apps/authentication/routes.py` and `apps/api/routes.py`

## Context

`tests/test_email_required.py` already covers the post-login e-mail flow
(`require_email`, `confirm_email`, `resend_email_code`, the OAuth `callback`
missing-email path, and the login->`/email/required` redirect). The three
delete endpoints (`delete_user`, `delete_group`, `delete_lab_category`) are
covered by `test_users.py` / `test_groups.py` / `test_lab_categories.py`. The
rest of both blueprints is untested.

This plan adds five new pytest files that follow the established approach
(session-scoped temp SQLite DB, `clabernetes` import stub, ordered/stateful
classes, unique username prefixes, `login`/`logout` helpers). Reuse verbatim
the harness boilerplate + the `no_mail_server` / `mail_outbox` monkeypatch
fixtures from `tests/test_email_required.py` (lines 34-145) for the mail-driven
auth tests.

### Shared-DB isolation & cache

All modules share ONE singleton app/DB **and one SimpleCache** (module-level
`apps.cache`). So:
- unique username prefixes per file (`ar`, `as`, `aa`, `ae`, `ak` below);
- any test that asserts on a cache-backed value (`user_likes`,
  `user_feedbacks`) must `cache.delete(<key>)` at the start to neutralise
  cross-module leakage.

### Untested routes

**auth** (`apps/authentication/routes.py`): `route_default`, `login`
(GET form / bad-password / already-authenticated branches), `login_oauth`,
`register`, `confirm_page`, `resend_code`, `reset_password`,
`confirm_reset_password`, `reload_profile`, and the error handlers.

**api** (`apps/api/routes.py`): `get_pods`, `get_lab_status`, `delete_lab`,
`get_nodes`, `bulk_approve_users`, `get_lab_answers`, `save_lab_answers`,
`save_grades_comments`, `join_group`, `check_lab_answer`, `feedback`,
`add_user_like`, `del_user_like`, `extend_lab`, `list_kubernetes_templates`,
`get_kubernetes_template`.

### Mocking

- Mail: reuse `no_mail_server` (fast path) and `mail_outbox` (captures
  `auth_routes.mail.send`) fixtures.
- k8s: `monkeypatch.setattr("apps.api.routes.k8s.<fn>", ...)` for the happy
  paths only (`validate_token`, `get_pods_by_lab_id`, `get_resources_by_name`,
  `delete_resources_by_name`, `get_nodes`). Permission/validation branches
  return before any k8s call.
- git templates: `monkeypatch.setattr("apps.api.routes.git.<fn>", ...)`
  (`update_repo`, `list_files`, `get_file`).
- OAuth: `monkeypatch.setattr(auth_routes.oauth.provider, "authorize_redirect", ...)`
  (as `test_email_required.py` already does for `authorize_access_token`).

Forms (no recaptcha; CSRF disabled via `WTF_CSRF_ENABLED=False`):
`CreateAccountForm` needs `username,email,password,terms` (send `terms="y"`);
`ConfirmAccountForm` needs `confirmation_token`; `ResetPasswordForm` needs
`identifier`; `ResetPasswordConfirmForm` needs `password`+`password_confirm`
(min 8, must match).

---

## File 1: `tests/test_auth_register.py`  (prefix `ar`)

Covers `register`, `confirm_page`, `resend_code`.

`register`:
- GET -> 200 registration form.
- POST invalid form (missing `terms`) -> 200 "Failed to validate form".
- POST existing username -> "Username already registered".
- POST existing email -> "Email already registered".
- POST valid with `no_mail_server` -> 302 to login; a `Users` row exists
  (username/email lower-cased, `issuer="LOCAL"`).
- POST valid with `mail_outbox` -> 302 to `/confirm`; `len(outbox)==1`; user is
  NOT yet created (`session['user']`/`confirmation_token` stashed instead).

`confirm_page` (drive it right after the register token path, same client):
- GET with no `confirmation_token` in session -> 302 to `/register`.
- POST wrong token -> "Invalid token"; user still not created.
- POST expired (`session['datetime']` set to >EXPIRY ago) -> "Token expired".
- POST correct token -> 302 to login; `Users` row now created and session keys
  popped.

`resend_code`:
- with no `user`/`token` in session -> 302 to `/confirm` (stashes error_msg).
- with a live registration session + `mail_outbox` -> 302 to `/confirm`;
  outbox grew by one.

---

## File 2: `tests/test_auth_session.py`  (prefix `as`)

Covers `route_default`, `login` (uncovered branches), `login_oauth`,
`reload_profile`, and the logout redirect. Seed one active user.

- `route_default`: GET `/` -> 302 to `/login/`.
- `login` GET unauthenticated -> 200 login form.
- `login` POST bad password -> 200 "Wrong user or password"; a
  `LoginLogging(success=False)` row is written.
- `login` GET while already authenticated -> 302 to the home index.
- `login_oauth`: GET `/login/oauth` with `oauth.provider.authorize_redirect`
  monkeypatched to return a redirect -> 302.
- `reload_profile`: logged-in GET `/profile/reload` -> 302 to `route_default`.
- `logout`: logged-in GET `/logout` -> 302 to `/login/`; afterwards a
  protected page redirects back to login (session cleared).
- (optional) `unauthorized_handler`: GET a protected page while logged out ->
  302 to login and `session['next_url']` is set.

---

## File 3: `tests/test_auth_password_reset.py`  (prefix `ap`)

Covers `reset_password` and `confirm_reset_password`. Seed an active user and a
soft-deleted user.

`reset_password`:
- POST invalid form (no `identifier`) -> 200 with validation error.
- POST unknown identifier -> 200 generic "If the user provided is valid..."
  message (no user enumeration), no mail sent.
- POST valid identifier with `mail_outbox` -> 200 generic message; `len(outbox)==1`.

`confirm_reset_password` (seed the cache token directly:
`cache.set(f"resetpw-{token}", user_id, timeout=3600)`):
- GET/POST with a token not in cache -> "Invalid or expired token".
- POST valid token, mismatched passwords -> form error (passwords must match).
- POST valid token, matching >=8-char passwords -> "Password changed
  successfully"; the user can now log in with the new password and the old one
  fails; the cache token is consumed.
- POST valid token whose user is soft-deleted -> "Invalid password reset request".

---

## File 4: `tests/test_api_lab_answers.py`  (prefix `ae`)

Covers `get_lab_answers`, `save_lab_answers`, `save_grades_comments`,
`check_lab_answer`. Seed: admin, teacher (owner of a group), student (member),
a Lab allowed to that group, a `LabInstance` owned by the student, a
`LabAnswerSheet`, and `LabAnswers`.

`get_lab_answers` (GET `/api/lab_answers/<lab_inst_id>`):
- category `user` -> 404; missing instance -> 404; non-owner -> 401;
  owner -> 200 with the answers dict.

`save_lab_answers` (POST JSON `/api/lab_answers/<lab_inst_id>`):
- `user` -> 404; empty body -> 400; missing instance -> 404; non-owner -> 401;
  owner POST answers -> 200 and a `LabAnswers` row created/updated (verify
  `answers_dict`); re-POST updates the same row.

`save_grades_comments` (POST JSON `/api/lab_answers/grades_comments/<answer_id>`):
- non-admin/teacher -> 401; missing answer -> 404; teacher who doesn't own the
  group -> 401; admin empty body -> 400; admin grade out of range (e.g. 150)
  -> 400 "Invalid grade"; admin valid -> 200 and `grades`/`comments` persisted.

`check_lab_answer` (GET `/api/lab_answers/check/<lab_id>/<answer_id>`):
- non-admin/teacher -> 401; unknown lab -> 401; lab not in a privileged group
  -> 401; unknown answer -> 401; lab without a sheet -> 400; valid -> 200 with a
  computed score string (reuse the scoring seeds from `test_lab_answers.py`).

---

## File 5: `tests/test_api_engagement.py`  (prefix `an`)

Covers `feedback`, `add_user_like`, `del_user_like`, `join_group`,
`extend_lab`, and `bulk_approve_users`. Seed: admin, teacher, an unapproved
`user`-category account, a student, a group with an `accesstoken`, a
`LabInstance` owned by the student.

`feedback` (`/api/feedback`), `cache.delete("user_feedbacks")` first:
- category `user` -> 401; GET -> 200 `recent_feedbacks`; POST without `stars`
  -> 400; POST with stars -> 200 and a `UserFeedbacks` row (created then
  updated on a second POST, no duplicate).

`add_user_like` / `del_user_like` (`/api/user_like`), `cache.delete("user_likes")` first:
- POST creates a `UserLikes` row and returns an incremented counter; POST again
  (already liked) returns the counter unchanged; DELETE removes it and floors
  the counter at 0.

`join_group` (POST JSON `/api/groups/join/<group_id>`):
- empty body -> 400; unknown group -> 404; SYSTEM group as non-admin -> 404;
  wrong `accessToken` -> 400; correct token -> 200 and membership added;
  re-join -> 200 "Already member of group".

`extend_lab` (POST JSON `/api/lab/<lab_id>/extend`):
- missing instance -> 404; non-owner non-admin -> 401; empty/`extend_hours`-less
  body -> 400; out-of-range hours (0 or >720 or non-int) -> 400; valid ->
  200 and `expiration_ts` updated.

`bulk_approve_users` (POST JSON `/api/users/bulk-approve`):
- non-admin/teacher -> 401; empty body -> 400; a non-`user` / invalid id in the
  list -> 400 with the error list; a valid list of `user`-category ids -> 200
  and those users flipped to `student`.

---

## File 6: `tests/test_api_k8s.py`  (prefix `ak`)

Covers the k8s/git-backed endpoints, stubbing only the happy paths. Seed:
admin, an unapproved `user`, a student, a `LabInstance` owned by the student.

`get_pods` (GET `/api/pods/<lab_id>`):
- `user` -> 404; no/blank Authorization header -> 400; token rejected
  (`validate_token` -> False) -> 404; valid (`validate_token` -> True,
  `get_pods_by_lab_id` -> [...]) -> 200.

`get_lab_status` (GET `/api/lab/status/<lab_id>`):
- `user` -> 404; missing instance -> 404; non-owner -> 401; owner with
  `get_resources_by_name` -> [...] -> 200.

`delete_lab` (DELETE `/api/lab/<lab_id>`):
- `user` -> 404; missing instance -> 404; non-owner non-admin -> 401; owner with
  `delete_resources_by_name` -> [] (empty resources) -> 200 and instance
  `is_deleted`/`finish_reason` set.

`get_nodes` (GET `/api/nodes`):
- `user` -> 404; `get_nodes` -> [ {name,latitude,longitude,status} ] -> 200.

`list_kubernetes_templates` (GET `/api/templates/list`, no auth required):
- with `LAB_TEMPLATES_GIT_URL` unset (default) -> 200 `{"status":"not-defined"}`;
  with it set + `git.update_repo`/`git.list_files` monkeypatched -> 200 list;
  `git.list_files` raising -> 400.

`get_kubernetes_template` (GET `/api/templates/<name>`):
- non-admin/teacher -> "Unauthorized request"; admin with `git.get_file` ->
  `(True, "yaml")` -> 200; `(False, "err")` -> 400.

---

## Files to create
- `tests/test_auth_register.py`
- `tests/test_auth_session.py`
- `tests/test_auth_password_reset.py`
- `tests/test_api_lab_answers.py`
- `tests/test_api_engagement.py`
- `tests/test_api_k8s.py`

No production changes are expected. If any handler surfaces a real bug (as the
`delete_group` `dict_keys` issue did earlier), stop and confirm the fix before
asserting a happy path.

## Verification
```
pip install -r requirements-dev.txt
pytest tests/test_auth_register.py tests/test_auth_session.py \
       tests/test_auth_password_reset.py tests/test_api_lab_answers.py \
       tests/test_api_engagement.py tests/test_api_k8s.py -v
pytest tests/ -v      # whole suite stays green
pytest tests/ --cov=apps.authentication.routes --cov=apps.api.routes \
       --cov-report=term-missing
```
