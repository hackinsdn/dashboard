# Playbook: building a Flask LTI 1.3 tool with Moodle integration

A reusable implementation plan distilled from building this demo. Follow the
phases in order — each was validated against Moodle 4.5 (docker, self-signed
HTTPS) and the ordering front-loads the integration risks.

## Stack

| Piece | Choice | Why |
|---|---|---|
| Web framework | Flask 3 | minimal, pylti1p3 ships a Flask adapter |
| LTI library | PyLTI1p3 2.x | implements OIDC login, launch validation, AGS, NRPS, deep linking |
| Launch/nonce cache | Flask-Caching (SimpleCache) | pylti1p3 needs server-side storage for state/nonce/launch data |
| Local data | SQLite (stdlib `sqlite3`) | users + registration tokens, zero setup |
| Keys | RSA 2048 (openssl) | tool signs token requests; platform verifies via JWKS or pasted PEM |

Single `app.py`, `templates/` (base/signin/signup/dashboard), `configs/`
(`lti_config.json`, `private.key`, `public.key`).

## Phase 1 — Core LTI 1.3 skeleton

Endpoints: `/login/` (OIDC initiation; log incoming `iss`/`client_id`/
`target_link_uri` and the outgoing redirect — these logs solve most
integration failures), `/launch/` (message launch), `/jwks/` (public keyset).

- Config file `lti_config.json` keyed by **issuer = Moodle base URL, exact
  string** (scheme/host/port, no trailing slash); per-issuer list of
  `{client_id, auth_login_url, auth_token_url, key_set_url, deployment_ids,
  private/public key files}`. Moodle endpoints are always
  `/mod/lti/{auth,token,certs}.php`.
- Generate keys: `openssl genrsa -out private.key 2048 && openssl rsa -in
  private.key -pubout -out public.key`.
- Use `enable_check_cookies()` on the OIDC login: pylti1p3 then detects
  blocked third-party cookies and falls back to opening a new window
  (`lti1p3_new_window=1` — two hits on `/login/` per launch is normal).

**Cookies:** iframe embedding needs `Secure; SameSite=None`, which breaks
plain-HTTP standalone use — make it env-gated (`COOKIES_SECURE=1`), default
`Lax`.

**TLS in dev:** every tool→platform call (keyset, token, AGS, NRPS) verifies
certs. Inject one `requests.Session` into every pylti1p3 constructor
(`requests_session=`) and gate `session.verify = False` behind
`INSECURE_SSL=1`; document `REQUESTS_CA_BUNDLE` as the proper alternative.

## Phase 2 — Dual mode: local accounts + shared dashboard

- `users` table: `id, username (unique), password_hash, display_name, email,
  lti_user_id (unique, nullable)`. Werkzeug password hashing.
- Routes `/signup/ /signin/ /logout/`; both auth paths set the same session
  shape (`mode: "local" | "lti"`, `user: {...}`) and land on one
  `/dashboard/`; LTI-only features render conditionally on `mode`.
- **Do not store the LTI claim set in the session** — Flask sessions are a
  ~4KB cookie and the claims exceed it silently. Store `launch_id` and
  reload claims via `FlaskMessageLaunch.from_cache()`.

## Phase 3 — LTI auto-provisioning of local users

On launch, `get_or_create_lti_user(launch_data)`:

- Identity key `f"{iss}|{sub}"` (sub is only unique per platform).
- First launch: create with username derived from email prefix → name slug →
  sub, sanitized, numeric suffix on collision; **empty password_hash** (LTI-
  only entry; signin rejects passwordless accounts with a pointer to Moodle).
- Later launches: load row and sync display_name/email from fresh claims.
- Migration: detect missing column via `PRAGMA table_info`, `ALTER TABLE`.

Plan account merging separately (same email → confirm + local password;
different email → emailed one-time token) — see MERGE_PLAN.md.

## Phase 4 — Message types beyond resource launch

- **Deep linking**: `is_deep_link_launch()` → respond with
  `DeepLinkResource` via `output_response_form()`.
- **Submission review** (Moodle's "View External tool results"): Moodle omits
  the spec-required `for_user` claim unless the link carries `?user=<id>`,
  and pylti1p3 rejects the launch. Subclass the validator to accept a
  missing `for_user` (keep every other check) and swap it in by patching
  `pylti1p3.message_launch.get_validators`. Show a review banner naming the
  reviewed user when the claim exists.

## Phase 5 — Grades (AGS) and roster (NRPS)

- APIs keyed by `launch_id` (client fetches with it; server restores the
  launch `from_cache` — no re-validation needed).
- `Grade`: score + timestamp + `activity_progress`/`grading_progress` +
  `user_id`; **`set_comment()` = gradebook feedback**. Omit the comment key
  entirely when blank.
- One score per user **per line item**; re-sending overwrites (history in
  Moodle's grade-history report). Multiple grades per user = extra line
  items: `LineItem` with a slugged `tag` (stable identity) + human `label`,
  `ags.put_grade(grade, lineitem)` — `find_or_create_lineitem` makes the
  gradebook column on first use. Resolve the line item **once per batch**.
- Batch endpoint accepts `[{user_id, score, comment}]` and reports
  success/error **per entry** (teachers/admins are non-gradable and 400 —
  don't abort the batch). AGS auth is tool-level (client credentials), so a
  teacher launch can grade any student; gate the UI by role in real tools.
- NRPS `get_members()` → include `user_id` (needed to send grades).

## Phase 6 — Dynamic registration (auto-configuration)

`/register/` endpoint (pylti1p3 has none — hand-rolled):

1. Moodle admin pastes the registration URL in Manage tools → "Add LTI
   Advantage"; Moodle opens it with `openid_configuration` (URL) and
   `registration_token` query params, **preserving existing query params**
   (verified in Moodle source — credentials can ride in the URL).
2. Tool fetches the platform's OpenID config (check the URL starts with the
   `issuer`), POSTs client registration (Bearer registration_token):
   `initiate_login_uri`, `redirect_uris`, `jwks_uri`,
   `token_endpoint_auth_method: private_key_jwt`, AGS+NRPS scopes, and the
   `lti-tool-configuration` claim (domain, target_link_uri, name/email
   claims, deep-linking message).
3. Response gives `client_id` (+ `deployment_id` inside
   lti-tool-configuration) → upsert into `lti_config.json` keyed by issuer
   (replace same client_id; first entry per issuer is `default`).
4. Return a page posting `{subject: 'org.imsglobal.lti.close'}` to the
   opener. Admin activates the pending tool in Moodle.

**Gate it** (the endpoint is otherwise open to anyone):

- One-time tokens (preferred): `registration_tokens` table storing SHA-256
  hashes with TTL; CLI `flask mint-registration-token --label X` prints the
  URL once; consume **only after successful registration** so failures are
  retryable; `list-registration-tokens` for audit.
- Static fallback: `REGISTRATION_KEY`/`REGISTRATION_SECRET` env vars checked
  with `hmac.compare_digest`. No credentials configured = endpoint disabled.

## Moodle setup checklist (manual path)

Tool URL/Redirection URI `https://<tool>/launch/`, initiate login
`/login/`, LTI 1.3, keyset `/jwks/`, deep linking on. Services: AGS **"grade
sync and column management"** (column management is required for extra
columns), NRPS on. Privacy: share name/email = Always. Copy client id +
deployment id into `lti_config.json`.

## Field guide: the failures you will hit, in order

1. **Moodle "Invalid request" at auth.php** → almost always `redirect_uri`
   not matching *Redirection URI(s)* character-for-character. Enable
   Moodle developer debugging for the real `error_description`; compare
   against the tool's `/login/` log line.
2. **`CERTIFICATE_VERIFY_FAILED` fetching `certs.php`** → tool doesn't trust
   Moodle's self-signed cert. `INSECURE_SSL=1` or `REQUESTS_CA_BUNDLE`.
3. **`token.php: 404` + Moodle TypeError in `jwks_helper::fix_jwks_alg`** →
   Moodle (in docker) can't fetch the tool keyset: `localhost` is the
   container. Paste the RSA public key instead of the keyset URL, or use
   `host.docker.internal`. Launches work before this because they only need
   tool→Moodle; token requests are the first Moodle→tool call.
4. **`scores: 400`** → grades are only accepted for users enrolled with a
   gradebook role (default: Student). Test as an enrolled student, not
   admin/teacher.
5. **`LtiException: For user claim must be included...`** → Moodle's
   submission-review launch without `?user=` (Phase 4 lenient validator).
6. **`gradesneedregrading`** after creating columns → open the **Grader
   report** to trigger recalculation, but by direct URL
   (`/grade/report/grader/index.php?id=<course>`): "Course → Grades"
   redirects to your last-used report, which may be the very page that
   throws. Recurs per new column; cron also clears it.

## Testing strategy (no Moodle required)

- Flask test client for auth flows, dashboards, guards.
- Unit-test payload builders (`Grade.get_value()`, line-item tag slugs) and
  the lenient validator against crafted JWT bodies (with/without/malformed
  `for_user`; resource launches unaffected).
- Dynamic registration: fake `requests.Session` returning a stub OpenID
  config + registration response; point `LTI_CONFIG_PATH` at a scratch copy;
  assert config upsert, token consumption, reuse/expiry rejection.
- Anything touching Moodle's server side (AGS/NRPS/regrade behavior) needs a
  real launch — keep the tool-side logs verbose enough to debug from them.

## Production deltas

Real TLS certs (drop `INSECURE_SSL`); real `SECRET_KEY`; Redis/DB cache
instead of SimpleCache (launch cache must survive restarts and multiple
workers); Postgres + migrations instead of SQLite; CSRF on all POSTs; role
checks on grading UI/APIs; rate-limit registration; per-institution key
rotation via keyset URL (needs the tool reachable from the platform);
implement account merging (MERGE_PLAN.md).
