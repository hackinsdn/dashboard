# Implementation Plan — LTI 1.3 integration for the HackInSDN Dashboard

Integrates the Dashboard as an LTI 1.3 tool (Moodle-first, platform-agnostic)
following the phases validated in [PLAYBOOK.md](PLAYBOOK.md), adapted to this
codebase: Flask app factory (`apps/create_app`), Flask-SQLAlchemy + Alembic
migrations, Flask-Login sessions, Flask-Caching, gunicorn.

Scope decisions:

- **Dynamic registration from day one** — `/lti/register/` is the way
  platforms are onboarded; there is no manual-registration phase or CLI.
  (An admin can still hand-craft an `lti_config` row in the DB if ever
  needed, but nothing is built around that.)
- **Multi-worker ready from day one** — production runs gunicorn with
  multiple workers, so all LTI shared state (OIDC state/nonce, launch data)
  lives in a shared cache backend, never in per-process memory.
- **Per-registration keys + retirement-based rollover from day one.**

## Stack

| Piece | Choice | Notes |
|---|---|---|
| LTI library | `PyLTI1p3` 2.x | ships the Flask adapter (`pylti1p3.contrib.flask`) |
| Launch/nonce/state storage | Flask-Caching via `FlaskCacheDataStorage`, **backed by Redis in production** | see "Multi-worker readiness" below |
| Tool config | **DB table `lti_config`** keyed by issuer | replaces the playbook's `lti_config.json`; DB is already shared across workers |
| Keys | RSA 2048 **per registration**, generated with `cryptography` | stored under `DATA_DIR/lti/keys/`, retired keys under `DATA_DIR/lti/keys/retired/` |

Dependency changes in `requirements.txt`: add `PyLTI1p3==2.0.0` (pulls
`pyjwt`, `jwcrypto`, `requests`) and `redis` (Flask-Caching's
`RedisCache` backend needs it).

## Multi-worker readiness (cross-cutting)

pylti1p3 stores OIDC `state`, `nonce` and the validated launch data
server-side between the `/login/` redirect and the `/launch/` POST — and
with multiple gunicorn workers those two requests can hit different
processes. `SimpleCache` (current default, per-process) breaks this
silently, so:

- `apps/config.py` grows cache-backend configuration:
  `CACHE_TYPE` (default `SimpleCache`) and `CACHE_REDIS_URL` env vars,
  passed through to the existing `apps.cache` instance. Production deploys
  set `CACHE_TYPE=RedisCache` + `CACHE_REDIS_URL=...` (the same Redis
  already used for `MESSAGE_QUEUE` can serve both).
- At blueprint load time, if the `lti` module is enabled while
  `CACHE_TYPE=SimpleCache` **and** more than one worker is possible, log a
  prominent WARNING pointing at this section (workers count isn't knowable
  from Flask, so warn on the cache type alone; dev single-worker use stays
  functional).
- Everything else LTI shares state through already-multi-worker-safe
  channels: tool config in the DB, key files on `DATA_DIR` (shared disk for
  workers on the same host), sessions in signed cookies.
- Registration concurrency: the `lti_config` upsert commits in one
  transaction keyed by the unique `issuer` column; a duplicate concurrent
  registration fails cleanly on the unique constraint and can be retried.

---

## Phase 1 — Blueprint skeleton + `lti_config` table

New package mirroring the existing blueprint pattern
(`apps/authentication`, `apps/clabs`):

```
apps/lti/
├── __init__.py      # blueprint = Blueprint('lti_blueprint', __name__, url_prefix='/lti')
├── routes.py        # /login/ /launch/ /jwks/ /register/  + CLI commands
├── models.py        # LtiConfig, LtiRegistrationToken
├── tool_conf.py     # DB-backed ToolConf for pylti1p3
└── keys.py          # key generation / retirement / rotation / JWKS assembly
```

Registration: ship as an **optional module** using the existing
`OPTIONAL_MODULES` mechanism (`OPTIONAL_MODULES=clabs,lti`), since LTI is
deployment-specific and the optional-module loader already logs and
tolerates failures. Defaults off; documented in `env-template`.

Resulting endpoints (trailing slashes matter — they are pasted into the LMS):

- `POST|GET /lti/login/` — OIDC third-party-initiated login
- `POST /lti/launch/` — message launch (tool redirect URI)
- `GET /lti/jwks/` — public keyset (active + retired keys)
- `GET|POST /lti/register/` — dynamic registration

pylti1p3 plumbing shared by the routes:

```python
tool_conf = DbToolConf()                       # tool_conf.py, reads lti_config table
launch_data_storage = FlaskCacheDataStorage(cache)   # apps.cache — Redis in prod
FlaskOIDCLogin(FlaskRequest(), tool_conf, launch_data_storage=...)
FlaskMessageLaunch(FlaskRequest(), tool_conf, launch_data_storage=...)
```

### `lti_config` model

`apps/lti/models.py`, following the `LabMetadata` JSON-in-text pattern:

```python
class LtiConfig(db.Model, AuditMixin):
    __tablename__ = "lti_config"
    id = db.Column(db.Integer, primary_key=True)
    issuer = db.Column(db.String(255), unique=True, nullable=False, index=True)
    _config = db.Column(db.Text, nullable=False)   # JSON list, property accessor "config"
    is_deleted = db.Column(db.Boolean, default=False)
```

- **`issuer` = LMS base URL, exact string** (scheme/host/port, **no trailing
  slash**) — must match the `iss` claim character-for-character. Normalize
  on write (strip trailing `/`), never on read.
- `_config` holds the JSON-encoded **per-issuer list** of registrations:

```json
[{
  "default": true,
  "client_id": "...",
  "auth_login_url": "https://<lms>/mod/lti/auth.php",
  "auth_token_url": "https://<lms>/mod/lti/token.php",
  "key_set_url":    "https://<lms>/mod/lti/certs.php",
  "deployment_ids": ["1"],
  "private_key_file": "lti/keys/<issuer-hash>_<client_id>.key",
  "public_key_file":  "lti/keys/<issuer-hash>_<client_id>.pub"
}]
```

Key paths are stored **relative to `DATA_DIR`** so DB rows survive host
moves.

`tool_conf.py`: subclass `pylti1p3.tool_config.ToolConfDict`. Constructor
queries all non-deleted `LtiConfig` rows, builds the
`{issuer: [registration, ...]}` dict, then calls
`set_private_key(...)`/`set_public_key(...)` per registration reading the
files under `DATA_DIR`. Instantiate per request (cheap; every worker picks
up new registrations without restart — required for multi-worker, where the
worker that served `/register/` is not necessarily the one serving the next
launch).

**Migration**: one Alembic revision following the repo convention —
`migrations/versions/2.0.10_add_lti_2.0.11.py`, `revision='2.0.11'`,
`down_revision='2.0.10'`, creating **both** `lti_config` and
`lti_registration_tokens` (Phase 2). Fresh installs get the tables via
`dbinit.py`'s `db.create_all()` (import `apps.lti.models` from
`apps/lti/routes.py`, which the optional-module loader imports, so the
models register with the metadata whenever the module is enabled).

The first registration for an issuer is marked `default`; a re-registration
with the same `client_id` replaces that entry instead of appending.

## Phase 2 — Keys, `/jwks/`, and dynamic `/register/`

This phase is the onboarding path — it lands before the launch flow is
useful, since without a registration there is nothing to launch.

### Key management (`apps/lti/keys.py`)

- `generate_keypair(issuer, client_id)` — RSA 2048 via `cryptography`
  (already an installed transitive dep; no `openssl` shell-out), writes
  `DATA_DIR/lti/keys/<sha256(issuer)[:12]>_<client_id>.key/.pub` with mode
  `0600`, returns the relative paths for the config entry. **Every
  registration gets its own fresh pair** — no shared tool-wide key, so one
  platform's key compromise or rotation never touches another's.
- `retire_key(path)` — moves both files into `DATA_DIR/lti/keys/retired/`,
  prefixing a timestamp so grace-period cleanup is mechanical.
- `rotate_key(issuer, client_id)` — the publish-then-switch rollover as one
  operation: generate the new pair (now in `/jwks/` alongside the old one),
  update the registration's key paths in `lti_config`, retire the old pair
  (still served from `retired/`). Exposed as a CLI command on the blueprint
  (`flask lti rotate-key --issuer ... --client-id ...`), same
  `blueprint.cli` pattern as `apps/cli/routes.py`.
- `build_jwks()` — assembles the keyset from **all active public keys
  referenced by `lti_config` rows plus every `.pub` in `retired/`**, each
  with a stable `kid` (thumbprint of the public key). Because retired keys
  stay published, platforms validating cached tokens keep finding the old
  `kid` throughout the grace period; delete from `retired/` afterwards
  (document ~30 days; a `flask lti purge-retired-keys --older-than-days N`
  command makes it a cron one-liner).

`GET /lti/jwks/` returns `{"keys": build_jwks()}`. Cache for ~5 min with
`apps.cache`; keying by cache backend means all workers serve the same
keyset, and a rotation is visible everywhere within the TTL (acceptable —
rotation publishes the new key *before* switching, so a stale cached keyset
still contains every key in use... provided rotation waits one TTL between
"publish" and "switch"; `rotate_key` sleeps/documents that or simply
invalidates the cache key after commit — do the cache invalidation, it's
one line).

### `GET|POST /lti/register/` — dynamic registration

pylti1p3 has none; hand-rolled per playbook Phase 6:

1. LMS admin pastes the credentialed URL into "Add LTI Advantage"; the LMS
   calls it with `openid_configuration` + `registration_token` query params,
   preserving our query-string credentials.
2. Validate credentials (below), fetch the platform OpenID config (verify
   the URL starts with the advertised `issuer`), **generate the
   per-registration keypair**, POST the client registration
   (`initiate_login_uri=/lti/login/`, `redirect_uris=[/lti/launch/]`,
   `jwks_uri=/lti/jwks/`, `token_endpoint_auth_method=private_key_jwt`,
   name/email claims, the `lti-tool-configuration` claim) using `BASE_URL`
   from config for absolute URLs.
3. Upsert the `lti_config` row for the issuer (replace same `client_id`,
   first entry = `default`), commit, and return the page posting
   `{subject: 'org.imsglobal.lti.close'}` to the opener. If registration
   fails after key generation, delete the orphan key files.

**Gating** (endpoint disabled when neither is configured):

- One-time tokens (preferred): `LtiRegistrationToken` model (SHA-256 hash,
  label, TTL, `used_at`), minted by
  `flask lti mint-registration-token --label "Uni X" --ttl-hours 24`;
  consumed only after a *successful* registration so failures are
  retryable; a `list-registration-tokens` command for audit.
- Static fallback: `LTI_REGISTRATION_KEY`/`LTI_REGISTRATION_SECRET` env vars
  compared with `hmac.compare_digest`.

## Phase 3 — `/login/`, `/launch/`, user provisioning

### OIDC login and launch

`/login/` **must log** the incoming `iss`, `client_id`, `login_hint`,
`target_link_uri` and the outgoing redirect URL at INFO level via
`app.logger` — the playbook's field guide shows these lines resolve most
integration failures (redirect_uri mismatch, wrong issuer key, etc).
Call `.enable_check_cookies()` on the OIDC login so blocked third-party
cookies fall back to a new window (two `/login/` hits per launch is normal).

`/launch/` on success: provision/login the user (below) and redirect to
`home_blueprint.index`. Do **not** store the claim set in the session
(4 KB cookie limit) — if any page later needs claims, store only
`launch_id` and reload with `FlaskMessageLaunch.from_cache()` (which reads
the shared cache, so it works regardless of which worker gets the request).

Cookie prerequisite (config.py): iframe launches need
`SESSION_COOKIE_SAMESITE="None"` + `SESSION_COOKIE_SECURE=True`. Env-gate it
(e.g. `COOKIES_SECURE=1`) with the current behavior as default, since dev
runs plain HTTP. Document the "*New window* launch container" workaround for
HTTP testing.

TLS in dev: every tool→platform call verifies certs. Support
`REQUESTS_CA_BUNDLE` (document it) and an `INSECURE_SSL=1` escape hatch that
injects a `requests.Session(verify=False)` into the pylti1p3 constructors.

### User provisioning and role → category mapping

On successful `/lti/launch/`, `get_or_create_lti_user(launch_data)`:

- **Identity key = `(issuer, subject)`** — the `Users` table already has
  `subject` and `issuer` columns used by the OAuth flow. Lookup:
  `Users.query.filter_by(issuer=iss, subject=sub, is_deleted=False)`.
  (`sub` is only unique per platform, so unlike the OAuth callback at
  `apps/authentication/routes.py:108`, the filter must include `issuer`.)
- **First launch**: create `Users(subject=sub, issuer=iss, given_name=...,
  family_name=..., email=...)` — the model's `__init__` already generates
  `uid`/`username`, and the `after_insert` listener adds the user to the
  *Everybody* system group. No password: `Users.password` stays `NULL`, and
  the local login form already rejects passwordless accounts
  (`user.password and verify_pass(...)` at `routes.py:55`), so the account
  is LTI-only until the user sets a password via reset.
- **Later launches inherit the same record**: sync `given_name`,
  `family_name`, `email` from fresh claims; set `last_login = utcnow()`;
  `login_user(user)`; write a `LoginLogging(auth_provider="lti", login=sub,
  success=True)` row (the column is `String(10)` — use the literal `"lti"`,
  not the issuer URL).
- Run `check_pre_approved(user)` like the other auth paths, then apply the
  **role mapping** from the roles claim
  (`https://purl.imsglobal.org/spec/lti/claim/roles`):

  | LTI role URI contains | category |
  |---|---|
  | `membership#Learner` (Student) | `student` |
  | `membership#Instructor` (Teacher) | `teacher` |

  Apply as a **promotion-only** rule: change the category only when the
  current category is `user` or `student`→`teacher` (never demote `admin`,
  `labcreator`, or an existing `teacher` back to `student`). Categories are
  validated by `Users.validate_category`, so only these two mappings are
  emitted; unknown roles leave the category untouched.
- Post-login redirect: honor `session["next_url"]` like the other flows,
  else `home_blueprint.index`. Skip the `require_email` redirect only if the
  launch provided an email; otherwise reuse it.

## Phase 4 — Config, docs, tests

**`apps/config.py` / `env-template`**: `OPTIONAL_MODULES` docs mention
`lti`; new vars `CACHE_TYPE`/`CACHE_REDIS_URL` (multi-worker requirement),
`LTI_REGISTRATION_KEY/SECRET`, `COOKIES_SECURE`, `INSECURE_SSL` (dev only).
`BASE_URL` already exists and is reused for all absolute LTI URLs.

**Docs**: extend `doc/lti/README.md` with dashboard-specific setup (URLs now
under `/lti/…`, DB-based config instead of `lti_config.json`, CLI commands,
the Redis cache requirement for multi-worker deploys), keeping the Moodle
field table and the troubleshooting/field-guide sections.

**Tests** (`tests/`, existing pytest + Flask test client conventions):

- `/lti/register/`: fake `requests.Session` returning stub OpenID config +
  registration response; assert row upsert, per-registration key files
  created, orphan keys removed on failure, token consumed once,
  expired/reused token rejected, endpoint disabled without credentials.
- `/lti/jwks/` shape: active + retired keys merged, stable `kid`s; full
  `rotate_key` scenario — after rotation both old and new keys are
  published, the config row points at the new private key, and the jwks
  cache was invalidated; `purge-retired-keys` honors the age threshold.
- `DbToolConf` builds the pylti1p3 dict from seeded `lti_config` rows;
  issuer normalization (trailing slash stripped).
- Provisioning: first launch creates the user (Everybody group, no
  password); second launch with same `(iss, sub)` reuses the row and syncs
  profile; role mapping Student→`student`, Teacher→`teacher`;
  promotion-only (admin not demoted); `LoginLogging` written.
  Drive it by monkeypatching `FlaskMessageLaunch` to return crafted claim
  dicts — no LMS needed.
- `/lti/login/` logs `iss`/`client_id`/`target_link_uri` (caplog assertion)
  and 4xxs cleanly on unknown issuer; warning is logged when the module
  loads with `CACHE_TYPE=SimpleCache`.
- Multi-worker smoke check (manual, documented): run gunicorn with `-w 2` +
  Redis cache locally and complete a full login→launch round trip, proving
  state/nonce survive crossing workers.

End-to-end against a real Moodle (docker) once, following the field guide in
PLAYBOOK.md — cookie/state errors, `redirect_uri` mismatch and keyset
reachability can only be seen there. Register via `/lti/register/` (that
*is* the onboarding path now), then launch as an enrolled student and as a
teacher to verify both category mappings.

## Production notes

- **Redis cache is a deploy prerequisite for LTI** with multiple gunicorn
  workers (state/nonce/launch data must be shared); reuse the
  `MESSAGE_QUEUE` Redis instance.
- Key files live on `DATA_DIR`, which all workers on a host share; if the
  deployment ever spans hosts, `DATA_DIR` must be on shared storage (already
  true for uploads/sqlite today).
- Rate-limit `/lti/register/`; keys directory backed up with `DATA_DIR`;
  schedule `purge-retired-keys` in cron alongside the existing CLI jobs.
- Out of scope for this iteration (future work, per playbook Phases 4–5):
  deep linking, AGS grade passback, NRPS roster, submission review, account
  merging with existing local accounts (same-email case).

## Delivery

Single PR (the pieces are not independently useful once dynamic
registration is the only onboarding path), reviewed in the phase order
above: skeleton/model/migration → keys+jwks+register → login/launch/
provisioning → config/docs/tests.

---

# Follow-up feature: custom claim `next_url` (deep-link redirect)

Let the platform send the tool a post-launch destination via LTI custom
parameters, so an LMS activity can land the user directly on a specific
Dashboard page (e.g. a lab) instead of the home page.

## Claim source

Custom parameters configured on the platform (Moodle: the *Custom
parameters* field on the External tool / activity, one `key=value` per
line, e.g. `next_url=/labs/abc123`) arrive in the id_token under
`https://purl.imsglobal.org/spec/lti/claim/custom` as a flat dict of
string values. No pylti1p3 helper needed — read it from
`message_launch.get_launch_data()` like the roles claim.

## Changes (all in `apps/lti/routes.py` + tests + docs)

1. **Constant** `LTI_CUSTOM_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/custom"`
   next to the existing roles-claim constant.

2. **Validator `is_safe_redirect_url(url)`** — the security core. The value
   is attacker-influenceable (anyone who can edit an LMS activity, or a
   rogue platform), so redirecting to it unvalidated is an open redirect,
   and a `javascript:` URL would be script injection. Policy: **relative
   paths, or absolute URLs on the Dashboard's own host (BASE_URL) only**.
   Pre-checks on the raw value (before parsing):
   - must be a non-empty `str`;
   - reject any backslash (browsers treat `\` as `/`, which defeats
     naive prefix checks) and any whitespace/control characters.
   Then `urllib.parse.urlsplit(url)` and accept exactly two shapes:
   - **relative path**: starts with a single `/` and
     `scheme == "" and netloc == ""` — this also rejects `//host`
     (protocol-relative) and `javascript:` forms;
   - **absolute same-host URL**: `scheme` and `netloc` both equal to
     BASE_URL's (parse BASE_URL once, compare lowercased). Full-netloc
     equality is what keeps the classic bypasses out: userinfo tricks
     (`https://dashboard.example@evil.com/` has netloc
     `dashboard.example@evil.com`), lookalike subdomains
     (`dashboard.example.evil.com`), port swaps and http→https scheme
     downgrades all fail the exact match. No `startswith`/substring
     matching anywhere.
   Anything rejected is **logged at WARNING with the offending value and
   the issuer** and the launch proceeds to the default redirect — a bad
   `next_url` must never break the launch.

3. **Launch integration.** In `launch()`, after `login_user(user)`:
   extract `launch_data.get(LTI_CUSTOM_CLAIM, {})` (guard: must be a
   dict), take `custom["next_url"]`, validate, and on success store it in
   `session["next_url"]` — *overwriting* any stale value (the fresh launch
   intent wins) and *before* the e-mail check. Reusing the existing
   session mechanism (instead of redirecting directly) keeps both exits
   working with no extra branching:
   - user has an e-mail → the existing `if "next_url" in session` pop
     redirects to it;
   - user has no e-mail yet → they detour through `/email/required`, and
     the require-email confirmation flow already pops `session["next_url"]`
     (apps/authentication/routes.py) — the deep link survives the detour.
   Log one INFO line when a next_url is accepted (issuer + path) for the
   same debuggability reason as the /login/ lines.

4. **Tests** (`tests/test_lti.py`, new `TestCustomNextUrl` class using the
   existing `_FakeMessageLaunch`):
   - accepted: `/labs/abc`, `/labs/abc?tab=2`, and the absolute
     same-host form `BASE_URL + "/labs/abc"` → 302 Location equals the
     value, session emptied afterwards;
   - rejected (302 to home + warning in caplog):
     `https://evil.example/x`, `//evil.example`, `/\evil.example`,
     `javascript:alert(1)`, `..%2f` style non-`/`-prefixed values, empty
     string, non-string values (list/int), claim present but not a dict,
     and the same-host bypass attempts — userinfo
     (`https://<base-host>@evil.example/`), lookalike subdomain
     (`https://<base-host>.evil.example/`), wrong port and http→https
     scheme mismatch;
   - claim absent → unchanged default redirect (regression guard);
   - custom next_url overrides a pre-seeded stale `session["next_url"]`;
   - no-e-mail launch with valid next_url → redirects to
     `/email/required` while `session["next_url"]` still holds the value
     (assert via `client.session_transaction()`).

5. **Docs** (`doc/lti/DASHBOARD.md`): short section under "Users,
   accounts and roles" — how to set *Custom parameters* in Moodle
   (`next_url=/labs/...`), and the rule that only relative paths or
   absolute URLs on the Dashboard's own BASE_URL host are honored
   (external URLs are ignored and logged).

## Explicit non-goals

- No other custom parameters are interpreted yet (the extraction helper
  should still return the whole dict so future params — e.g. auto-starting
  a specific lab — reuse it).
- No `target_link_uri`-based deep linking and no LTI Deep Linking message
  type — this is only about the custom-parameters claim.
- No cross-host allowlist (e.g. sibling deployments or the LMS itself):
  the only absolute URLs honored are exact scheme+netloc matches against
  BASE_URL.
