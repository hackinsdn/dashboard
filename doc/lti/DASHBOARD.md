# LTI 1.3 integration — Dashboard setup guide

The Dashboard can act as an LTI 1.3 tool (tested against Moodle; the flow is
platform-agnostic). This guide covers the Dashboard-specific setup; the
generic background, field guide and troubleshooting live in
[README.md](README.md) and [PLAYBOOK.md](PLAYBOOK.md), and the design in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## 1. Enable the module

The integration ships as the optional module `lti`:

```bash
export OPTIONAL_MODULES=clabs,lti
```

Endpoints (paste these into the LMS; trailing slashes matter):

| Purpose | URL |
|---|---|
| OIDC initiate login | `https://<dashboard>/lti/login/` |
| Launch / redirect URI | `https://<dashboard>/lti/launch/` |
| Public keyset (JWKS) | `https://<dashboard>/lti/jwks/` |
| Dynamic registration | `https://<dashboard>/lti/register/` |

`BASE_URL` must be set to the Dashboard's public URL — it is used to build
the absolute URLs sent during dynamic registration.

Apply the DB migration (`lti_config` + `lti_registration_tokens` tables):

```bash
flask db upgrade
```

## 2. Production prerequisites

- **Shared cache (mandatory with multiple gunicorn workers).** The LTI OIDC
  state/nonce and launch data live in the Flask cache, and the `/login/` and
  `/launch/` requests may hit different workers. Configure Redis:

  ```bash
  export CACHE_TYPE=RedisCache
  export CACHE_REDIS_URL=redis://localhost:6379/0
  ```

  With `CACHE_TYPE=SimpleCache` (default) the module logs a warning at
  startup and launches only work single-worker (dev).

- **Cookies.** LMS launches render inside an iframe, which requires
  `Secure; SameSite=None` session cookies (HTTPS only):

  ```bash
  export COOKIES_SECURE=True
  ```

  For plain-HTTP testing leave it off and set the activity's *Launch
  container* to **New window** in Moodle.

- **TLS.** Every tool→platform call verifies certificates. For an LMS with
  a private CA set `REQUESTS_CA_BUNDLE=/path/to/ca.pem`; for local dev only,
  `INSECURE_SSL=True` disables verification.

## 3. Register a platform (dynamic registration)

Registration is done through `/lti/register/`, gated by credentials. Two
options:

**One-time token (recommended).** Mint one per LMS admin:

```bash
flask lti mint-registration-token --label "Uni X" --ttl-hours 24
```

It prints the registration URL to hand over. Tokens are stored hashed,
expire after the TTL and are consumed by the first *successful*
registration (failed attempts remain retryable). Audit with
`flask lti list-registration-tokens`.

**Static key/secret.** Set both env vars and share the URL:

```bash
export LTI_REGISTRATION_KEY=some-key
export LTI_REGISTRATION_SECRET=some-secret
# -> https://<dashboard>/lti/register/?key=some-key&secret=some-secret
```

With neither configured the endpoint is disabled.

In Moodle: *Site administration → Plugins → Activity modules → External tool
→ Manage tools*, paste the credentialed URL into the *Tool URL* box and
click **Add LTI Advantage**. The tool and Moodle exchange configuration
automatically; a fresh RSA-2048 keypair is generated for the registration
and the platform's client id/deployment id are stored in the `lti_config`
table. Activate the pending tool and review its Privacy settings
(name/email sharing must be on for account provisioning to get e-mails).

Each registration gets its own keypair (stored under `DATA_DIR/lti/keys/`,
paths recorded in `lti_config`); re-registering the same client id rotates
its keys automatically.

## 4. Users, accounts and roles

On the first launch the Dashboard auto-provisions a `Users` row keyed by
**issuer + subject**, so the same LMS person always maps to the same
account; name/e-mail are refreshed from the launch claims on every launch.
LTI accounts have no password (the local sign-in form rejects them; the
password-reset flow can add one later). If the launch e-mail appears in a
group's pre-approved users list, the new account is joined to that group
automatically at creation (see `sync-pre-approved-users` in
[doc/DEV.md](../DEV.md) for backfilling pre-existing accounts).

Role mapping is **promotion-only**:

| LTI role (membership vocabulary) | Dashboard category |
|---|---|
| Learner (Student) | `student` |
| Instructor (Teacher) | `teacher` |

A Learner launch never demotes an existing `teacher`, `labcreator` or
`admin`; an Instructor launch promotes `user`/`student` to `teacher`.

Logins appear in the `login_logging` table with `auth_provider=lti`, and the
`/lti/login/` log lines include `iss`, `client_id` and `target_link_uri` —
compare them with the LMS tool configuration when a launch fails (this
solves most integration problems; see the field guide in PLAYBOOK.md).

### Deep links (custom parameter `next_url`)

An LMS activity can land the user on a specific Dashboard page after the
launch. In Moodle, set the activity's (or tool's) **Custom parameters**
field to e.g.:

```
next_url=/labs/abc123
```

The value arrives in the LTI custom claim and is honored only if it stays
on this Dashboard: a relative path, or an absolute URL whose scheme and
host:port exactly match `BASE_URL`. External URLs (and tricks like
`//host`, `javascript:`, userinfo `@` or lookalike hosts) are ignored and
logged with a warning — the launch then falls back to the home page. The
destination also survives the first-launch e-mail confirmation detour.

## 5. Grade passback (AGS)

When an LTI-launched user finishes a lab (the congratulations page), the
Dashboard automatically posts the result to the course gradebook via LTI
Assignment and Grade Services:

- the user's **answers** are always sent as the gradebook feedback
  comment, one line per question — including questions left unanswered
  (listed as "(not answered)");
- a **score** (0–100) is sent only when the lab has an **answer sheet**
  registered (same computation as the teachers' "check with answer sheet"
  listing: regex matching plus manual-grade overrides); labs without a
  sheet post the comment with grading progress *PendingManual* so the
  teacher can grade in Moodle;
- the AGS context is captured **at launch time**, so a user must have
  launched through the LMS at least once (after this feature is deployed)
  before their finishes can be graded; refreshing the finished page
  re-sends and simply overwrites the same gradebook cell (Moodle keeps
  grade history);
- when the launch carries no default gradebook line item (Moodle with
  plain "grade sync", course-level tools), the column is located in the
  platform's line-item collection by resource link — and created on the
  fly when the platform granted column management (the full `lineitem`
  scope, requested by our dynamic registration).

Setup notes:

- Dynamic registration now requests the AGS scopes automatically.
  **Platforms registered before this feature** must have the service
  enabled by hand: edit the tool in Moodle and set *IMS LTI Assignment and
  Grade Services* to "Use this service for grade sync" — or simply
  re-register.
- For grades to land in the right column, create **one External tool
  activity per lab** and point it at the lab with the `next_url` custom
  parameter (section above); the Dashboard prefers the launch context
  whose deep link matches the finished lab, falling back to the most
  recent launch.
- Moodle only accepts scores for users enrolled with a gradable role —
  a teacher/admin launching and finishing a lab gets a rejected score
  (logged as INFO, page unaffected). Test with an enrolled student.

## 6. Key rollover

Publish-then-switch, per registration:

```bash
flask lti rotate-key --issuer https://moodle.example.edu [--client-id X]
```

The new key is generated and published in `/lti/jwks/`, the registration
switches to signing with it, and the old pair moves to
`DATA_DIR/lti/keys/retired/` — its public half **stays published** so the
platform keeps validating cached tokens. After the grace period:

```bash
flask lti purge-retired-keys --older-than-days 30   # cron-friendly
```

## 7. Out of scope (for now)

Deep linking, roster (NRPS), submission review and merging LTI accounts
with pre-existing local accounts. See the future-work notes in
IMPLEMENTATION_PLAN.md and GRADE_PASSBACK_PLAN.md.
