# Release Notes — v2.0.13

This release delivers a large batch of new features across lab management, an
LTI 1.3 integration, a support-chat system, full internationalization, plus
numerous fixes, dependency upgrades, and a big jump in automated test coverage.

## Highlights

- **Internationalization (EN + pt_BR)** — the whole UI is now translatable, with a
  navbar language switcher and a per-user language preference.
- **LTI 1.3 integration** — HackInSDN can now be embedded as an external tool in
  Moodle/LMS platforms, including grade passback.
- **Support chat** — an in-app chat widget with admin thread management and open-case
  badges.
- **Lab lifecycle improvements** — fork existing labs, soft-delete with admin
  restore, categories CRUD, field version control, custom display ordering, and
  group-filtered views.
- **File upload** support for labs.
- **Operational hardening** — Gunicorn deployment, kubectl delete timeouts, and
  more robust Kubernetes service-pod resolution via the Discovery API.
- **User feedback** -- The Dashboard now have a involuntary feedback modal to gather
  user's feedback on the home page, which helps the continuosly improvement.

## Features & Enhancements

| PR | Description |
|----|-------------|
| [#226](https://github.com/hackinsdn/dashboard/pull/226) | Show feedback modal when the user accesses the app |
| [#229](https://github.com/hackinsdn/dashboard/pull/229) | Add tables support to markdown rendering |
| [#231](https://github.com/hackinsdn/dashboard/pull/231) | Add options to reload or cancel the lab instance view |
| [#236](https://github.com/hackinsdn/dashboard/pull/236) | Serve the app with Gunicorn |
| [#239](https://github.com/hackinsdn/dashboard/pull/239) | Start checking questions for duplicates |
| [#241](https://github.com/hackinsdn/dashboard/pull/241) | Add support for file upload |
| [#245](https://github.com/hackinsdn/dashboard/pull/245) | View labs filtered by group |
| [#247](https://github.com/hackinsdn/dashboard/pull/247) | Add CRUD for Lab Categories |
| [#248](https://github.com/hackinsdn/dashboard/pull/248) | Require e-mail confirmation after login when missing |
| [#255](https://github.com/hackinsdn/dashboard/pull/255) | Soft-delete and admin restore for catalog Labs |
| [#256](https://github.com/hackinsdn/dashboard/pull/256) | Support chat widget with admin thread management |
| [#260](https://github.com/hackinsdn/dashboard/pull/260) | Fork an existing Lab |
| [#261](https://github.com/hackinsdn/dashboard/pull/261) | Add user-provided notes to help admin approval |
| [#262](https://github.com/hackinsdn/dashboard/pull/262) | Support chat: sidebar badge shows open cases; remove inactivity auto-finish |
| [#263](https://github.com/hackinsdn/dashboard/pull/263) | Rename "duplicate lab" to "fork lab" |
| [#264](https://github.com/hackinsdn/dashboard/pull/264) | Elapsed-wait timer and staged messages on the Run Lab Status page |
| [#265](https://github.com/hackinsdn/dashboard/pull/265) | Order Labs listing by a `display_order` attribute |
| [#266](https://github.com/hackinsdn/dashboard/pull/266) | Version control for editable Lab fields (manifest, lab guide, extended description) |
| [#267](https://github.com/hackinsdn/dashboard/pull/267) | LTI 1.3 integration (optional module `lti`) |
| [#270](https://github.com/hackinsdn/dashboard/pull/270) | Resolve service pods via Discovery API endpoint slices |
| [#286](https://github.com/hackinsdn/dashboard/pull/286) | Internationalization with Flask-Babel: English + Brazilian Portuguese, navbar language switcher, per-user `locale` preference |
| [#290](https://github.com/hackinsdn/dashboard/pull/290) | Bulk delete of running Lab instances and of users (`DELETE /api/labs`, `DELETE /api/users/bulk`) |

## Fixes

| PR | Description |
|----|-------------|
| [#232](https://github.com/hackinsdn/dashboard/pull/232) | Refactor allowed characters on username when editing the user |
| [#243](https://github.com/hackinsdn/dashboard/pull/243) | Use `request.full_path` to avoid dropping the query string |
| [#250](https://github.com/hackinsdn/dashboard/pull/250) | Small bug fixes and handling of warnings/deprecated messages |
| [#258](https://github.com/hackinsdn/dashboard/pull/258) | `Users.created_at` can be null for old migrated data |
| [#259](https://github.com/hackinsdn/dashboard/pull/259) | Fix djlint errors in templates and add lint CI workflow |
| [#271](https://github.com/hackinsdn/dashboard/pull/271) | Add timeout to kubectl delete operations |
| [#286](https://github.com/hackinsdn/dashboard/pull/286) | Password-reset e-mails showed a literal `{url_for(...)}` instead of the reset link (missing f-prefix) |

## Dependencies & Security

| PR | Description |
|----|-------------|
| [#240](https://github.com/hackinsdn/dashboard/pull/240) | Upgrade gitpython, requests, python-dotenv and werkzeug |
| [#246](https://github.com/hackinsdn/dashboard/pull/246) | Bump flask from 3.1.2 to 3.1.3 |
| [#249](https://github.com/hackinsdn/dashboard/pull/249) | Bump pytest from 8.3.4 to 9.0.3 |
| [#251](https://github.com/hackinsdn/dashboard/pull/251) | Bump black from 24.3.0 to 26.3.1 |

## Testing & CI

| PR | Description |
|----|-------------|
| [#252](https://github.com/hackinsdn/dashboard/pull/252) | Add unit test suite and CI coverage workflow |
| [#254](https://github.com/hackinsdn/dashboard/pull/254) | Add Kubernetes controller tests from API tape (coverage 61% → 83%) |

## Upgrade Notes

- This release includes new Alembic migrations up to **2.0.14**
  (`2.0.11_add_lti_2.0.12`, `2.0.12_add_lti_launch_context_2.0.13` and
  `2.0.13_add_users_locale_2.0.14`). Run `flask db upgrade` after deploying.
- The LTI integration is shipped as an **optional module** (`lti`); enable it only
  where needed.
- **Translations must be compiled** before the app can serve a non-default locale.
  The Docker build runs this automatically; for source deployments run
  `make i18n-compile` to build the `.mo` catalogs. The available locales are
  controlled by `LANGUAGES` (default `en,pt_BR`), and the fallback by
  `BABEL_DEFAULT_LOCALE` (default `en`). See [i18n.md](i18n.md) for the
  maintainer workflow.
