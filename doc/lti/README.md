# Flask LTI 1.3 Demo (Moodle integration + standalone local accounts)

A minimal Flask app that works **two ways**:

1. **Standalone** — users create local accounts (SQLite) and sign in at
   `/signin/`, no Moodle required.
2. **As an LTI 1.3 tool** — embedded in Moodle as an External Tool. On first
   launch a local account is **auto-provisioned** for the Moodle user (keyed
   by LTI issuer + subject, so the same person maps to the same account on
   every launch); the profile is refreshed from the launch data each time.
   These accounts have no password — they can only be entered via LTI.

LTI features demonstrated:

- OIDC third-party-initiated login (`/login/`)
- Resource link launch showing the user/course claims (`/launch/`)
- Public JWKS endpoint for signature verification (`/jwks/`)
- **Deep Linking** (content selection when a teacher adds the activity)
- **Grade passback** to the Moodle gradebook (Assignment and Grade Services)
- **Course roster** listing (Names and Role Provisioning Services)

## 1. Run the tool

```bash
python3 -m venv venv
source venv/bin/activate          # fish: source venv/bin/activate.fish
pip install -r requirements.txt
python app.py                     # listens on http://0.0.0.0:9001
```

**Standalone use:** open <http://localhost:9001/>, create an account at
`/signup/`, and sign in. The local user database is `users.db` (SQLite,
created automatically). Moodle-only features (grade passback, roster) are
hidden in this mode.

**Cookie note:** by default cookies are `SameSite=Lax` (works over plain HTTP
for standalone use). When serving the tool to Moodle over HTTPS inside an
iframe, start it with `COOKIES_SECURE=1 python app.py` so the session cookie
is `Secure; SameSite=None`.

RSA keys were already generated in `configs/` (regenerate with
`openssl genrsa -out configs/private.key 2048 && openssl rsa -in configs/private.key -pubout -out configs/public.key`).

> **HTTPS / cookies:** browsers block third-party cookies inside iframes, and
> `SameSite=None` cookies require HTTPS. For quick local testing either set the
> activity's *Launch container* to **New window** in Moodle, or expose the tool
> over HTTPS with e.g. `ngrok http 9001` and use the HTTPS URL everywhere below.

## 2. Register the tool in Moodle

### Option A — Dynamic registration (automatic, recommended)

Registration requires credentials — the endpoint is disabled without them.
Two options:

**One-time token (recommended).** Mint a token for each platform admin:

```bash
venv/bin/flask --app app mint-registration-token --label "Uni X" --ttl-hours 24
```

It prints the registration URL to hand over
(`https://<tool-host>/register/?token=...`). Tokens are stored hashed,
expire after the TTL, and are consumed by the first successful
registration — a leaked or reused URL is rejected afterwards. Inspect them
with `flask --app app list-registration-tokens`.

**Static key/secret** (LTI 1.1 called these "consumer key" / "shared
secret"). Start the tool with both set:

```bash
REGISTRATION_KEY=demo-key REGISTRATION_SECRET=demo-secret python app.py
```

and register with `https://localhost:9001/register/?key=demo-key&secret=demo-secret`.

Either way: Site administration → Plugins → Activity modules → External tool
→ **Manage tools** → paste the credentialed registration URL into the *Tool
URL* box → **Add LTI Advantage**. (Moodle keeps the URL's query parameters
when it calls the endpoint, so they act as the registration password.)

→ click **Add LTI Advantage**. The tool and Moodle exchange configuration
automatically (endpoints, scopes, deep linking, AGS/NRPS services) and the
client id + deployment id are written into `configs/lti_config.json` for you
— no restart needed. Back in Moodle, the tool appears as *Pending*: click
**Activate**. Then review the tool's **Privacy** settings (name/email sharing)
— dynamic registration requests those claims but it's worth confirming.

### Option B — Manual registration (field by field)

Same page → *configure a tool manually*:

| Field | Value |
|---|---|
| Tool name | Flask LTI Demo |
| Tool URL | `http://localhost:9001/launch/` |
| LTI version | LTI 1.3 |
| Public key type | Keyset URL |
| Public keyset | `http://localhost:9001/jwks/` |
| Initiate login URL | `http://localhost:9001/login/` |
| Redirection URI(s) | `http://localhost:9001/launch/` |
| Supports Deep Linking | ✔ (Content selection URL: `http://localhost:9001/launch/`) |

Under **Services**:
- IMS LTI Assignment and Grade Services: *Use this service for grade sync
  **and column management*** (column management is what lets the tool create
  extra gradebook columns for multiple grades per user)
- IMS LTI Names and Role Provisioning: *Use this service*

Under **Privacy**: share launcher's name and email with the tool.

After saving, open the tool's details (list icon on the tool card) and note:

- **Client ID**
- **Deployment ID**

## 3. Point the tool at your Moodle

Edit `configs/lti_config.json`:

- Replace the top-level key `http://localhost:8080` with your Moodle base URL
  (no trailing slash) — it must match the `iss` Moodle sends.
- Update the three `auth_login_url` / `auth_token_url` / `key_set_url` values with
  the same base URL (paths stay `/mod/lti/auth.php`, `/mod/lti/token.php`,
  `/mod/lti/certs.php`).
- Fill in `client_id` and `deployment_ids` from step 2.

Restart `python app.py`.

## 4. Add it to a course

In a course: *Add an activity or resource* → **External tool** → pick "Flask LTI
Demo" as the preconfigured tool (or use "Select content" if deep linking is
enabled). Launch it — you should see the launch page with the LTI claims, a
button to push a grade into the gradebook, and a button to list course members.

## Troubleshooting

- **"iss not found"**: the issuer key in `lti_config.json` must exactly match
  your Moodle URL (scheme, host, port, no trailing slash).
- **Cookie/state errors on launch**: use *New window* as launch container or
  serve the tool over HTTPS (see note above).
- **Grade button returns 403**: enable the Assignment and Grade Services option
  in the tool registration and ensure the activity has a grade configured.
- **"View External tool results" crashed with `For user claim must be
  included in a LtiSubmissionReviewRequest`**: Moodle sends that link as an
  LTI *submission review* launch but omits the spec-required `for_user` claim
  unless the link carries a `?user=<id>` parameter. This app patches in a
  lenient validator that accepts such launches and falls back to the
  launching user (see `LenientSubmissionReviewValidator` in `app.py`).
- **Moodle throws `gradesneedregrading` in Single view / grade export after
  the tool created gradebook columns**: creating columns via AGS flags the
  course gradebook for regrading, and some report pages refuse to render
  until it happens. Opening the **Grader report** triggers the
  recalculation — but beware: "Course → Grades" redirects to your
  *last-used* report, so if that was Single view you never get there. Open
  it directly: `https://<moodle>/grade/report/grader/index.php?id=<courseid>`
  (or run Moodle cron). Recurs whenever the tool creates a new column —
  harmless.
- **Grade button fails with `services.php/.../scores: 400`**: Moodle only
  accepts scores for users who are *enrolled in the course* with a graded role
  (by default, Student). Launching as an admin or teacher and sending a grade
  always returns 400. Launch the activity as an enrolled student (e.g. a test
  user in an incognito window) and send the grade from there.
- **Grade/roster calls fail with `LtiServiceException ... token.php: 404`**
  (Moodle logs a `TypeError` in `jwks_helper::fix_jwks_alg`): Moodle could not
  fetch the tool's public keyset to verify the token request. This always
  happens when Moodle runs in Docker and the keyset URL says `localhost` —
  inside the container that is the container itself, not your machine. Easiest
  fix: edit the tool in Moodle, set *Public key type* to **RSA key**, and paste
  the contents of `configs/public.key`. Alternatively keep *Keyset URL* but use
  an address reachable from the container (e.g.
  `https://host.docker.internal:9001/jwks/`) with a certificate Moodle trusts.
- **`CERTIFICATE_VERIFY_FAILED` fetching `/mod/lti/certs.php`**: the tool
  verifies launches by fetching Moodle's keyset server-side, and a self-signed
  Moodle certificate fails that check. Either point Python at the cert
  (`REQUESTS_CA_BUNDLE=/path/to/moodle-cert.pem python app.py` — export it with
  `openssl s_client -connect localhost:8443 </dev/null 2>/dev/null | openssl
  x509 > moodle-cert.pem`) or, for local development only, disable verification
  with `INSECURE_SSL=1 python app.py`.
