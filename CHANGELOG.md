# Changelog

All notable changes to this project are documented here, grouped by tagged version.

## [UNRELEASED] - Under development

### Fixed
- Make Lab teardown idempotent so it never orphans Kubernetes resources (#305).
- Fetch Kubernetes endpoints as raw JSON and parse them to avoid strict model
  validation errors that left orphan services with null endpoints (#303, #304).
- Require authentication and staff category for `/api/templates/list` (#307).

### Added
- Audit log messages for uploads, Labs and Lab Instances (#306).

### Security
- Remove the insecure Flask-Login `request_loader` that allowed an
  authentication bypass (#301).
- Fix several dependency vulnerabilities reported by Dependabot, including
  GitPython (#302).

## [2.0.14-3] - 2026-08-10

### Fixed
- Handle null timestamps on the finished_labs page (#300).

## [2.0.14-2] - 2026-08-03

### Added
- Configurable timeout for Kubernetes API calls (#292).
- Support for multiple kubeconfigs with failover in the Kubernetes controller
  (#293).
- Attach Lab data files to Kubernetes resources as ConfigMaps (#294).

### Fixed
- Make the Kubernetes request timeout actually take effect.
- Store the group expiration date as a datetime (#296).
- Replace the demo Contact page with real support channels (#295).
- Ignore unknown identifiers in `string.Template` for Kubernetes manifests
  (#298).
- Read the selected rows from the DataTable (follow-up to bulk delete, #290).

### Security
- Bump GitPython from 3.1.50 to 3.1.54 (#299).

## [2.0.14] - 2026-07-20

### Added
- Flask-Babel internationalization with English and pt_BR translations,
  externalizing user-facing strings across dashboard, users, groups, support,
  labs, feedback and other pages, plus a `window.i18n` catalog for static JS
  (#286).
- Bulk delete for running Lab instances and users (#290).
- Detect duplicate input ids in Lab guide questions (#284).
- Admin web UI for LTI management commands; LTI loaded as an optional module by
  default (#283).
- Delete a single Lab field version from the history modal, with colored
  version diffs (#280, #281).

### Fixed
- Correct config interpolation in the index.html and xterm.html templates
  (#291).
- Stop the xterm client from crashing on disconnect and forward the Kubernetes
  exit code (#289).
- Bump flask_socketio to 5.6.1 for Flask 3.1.3 compatibility (#288).
- Make Lab Instance answer loading more robust and add safe guards around
  saveAnswers to avoid losing unsaved answers (#278, #285).
- Correct the card filter to avoid layout gaps (#275).
- Detect duplicate question names when a select shares a radio/checkbox name
  (#273).

## [2.0.13] - 2026-07-09

### Added
- LTI 1.3 integration as an optional `lti` module, including dynamic
  registration with tool icon, automatic AGS grade/answers passback on Lab
  completion, `next_url` deep-link redirects, a `show-public-key` CLI, and
  troubleshooting docs (#266, #267).
- Version control for the Labs manifest, with a versions button and history UI
  (#266 branch work).
- Resolve Kubernetes service pods via the Discovery API endpoint slices (#270).
- Pre-approved group membership at user creation, with a CLI backfill sweep.

### Fixed
- Add a timeout to `kubectl delete` operations (#269, #271).
- Add newlines to better display grade feedback on Moodle.

## [2.0.10] - 2026-07-05

### Added
- Order the Labs listing by the `display_order` attribute (#265).
- Timer showing elapsed time on Lab instance status, with a color change when
  the elapsed time exceeds the limit (#264).

### Changed
- Rename the "duplicate lab" feature to "fork lab" (#263).
- Refine the support chat to avoid finishing threads by inactivity.

### Fixed
- Fix the `flush-support-emails` CLI so it no longer emails about finished
  threads and distinguishes staff-handled cases from never-seen ones (#262).

## [2.0.9] - 2026-07-05

### Added
- Support chat: admin thread management, unread badges, multi-line input,
  responsive widget, batched email notifications, telemetry and polling; admins
  can finish conversations (#256).
- Soft-delete and admin restore for catalog Labs (#255).
- Duplicate Labs feature, later reworked to avoid orphan files by eliminating
  the disk copy (#259, #260).
- CRUD for Lab Categories (#247).
- View Labs filtered by group (#245).
- Lab-guide file uploads, including upload removal (#241).
- User notes and a waiting-approval justification (#261).
- Post-login email collection: require email confirmation after login when
  missing (#248).
- ContainerLab topology visualizer and menu, upload flow, and Secrets support
  (#165, #214, #219).
- Unit test suite with CI coverage workflow, Kubernetes controller tests, and a
  raised coverage threshold (#251, #252, #254).
- Move towards Gunicorn for serving the app (#236).
- Last login and Created at columns for users, with stable sorting (#180).
- Persistent DataTables state and disabled autocorrect/autocomplete across
  users, running labs and lab-answers tables (#200, #204, #205).
- Force-refresh button for the git templates repository; load templates from a
  git repo (#166).
- Access token generation option, random or manual (#151).
- Require accepting terms on register and enforce a username policy (#146,
  #150).
- Spinner on the Run Lab button to prevent multiple clicks (#212).

### Changed
- Refactor the lab usage stats and category usage reports and the recently
  added labs chart (#176, #177, #183).
- Restrict teacher/lab-creator Lab deletion to their own Labs; add the
  lab-creator user category (#215).
- Migrate node geotag info to configuration; parameterize testbed title and map
  settings.

### Fixed
- Numerous small bug fixes and SQLAlchemy deprecation cleanups surfaced by the
  new tests (#250).
- Handle `Users.created_at` being null for old migrated data (#258).
- Fix ContainerLab file re-upload to overwrite previous content (#216).
- Fix the xterm terminal for mobile devices and dark/light theme, and keep it
  open on error (#197, #213).
- Fix access control for the labcreator category with ContainerLab labs (#224).
- Various djlint template lint fixes and a djlint CI workflow (#259 branch).

### Security
- Upgrade GitPython, requests, python-dotenv and Werkzeug; fix Dependabot
  security alerts (#201, #240).

## [2.0.1] - 2025-05-20

### Added
- Password reset / recovery flow with dedicated forms and routes (#96).
- Lab rating: like, comment and feedback features, with caching (#99).
- Dashboard statistics with dynamic resource-usage metrics and Kubernetes
  cluster stats (#112).
- List pods, deployments and services for a running Lab (#123).
- Finished-lab info page including lab_id, and a local issuer shown on the user
  profile (#118, #119).
- Real-time feedback application on the answer-sheet view (#113).

### Changed
- Update the stats shown on finished labs.

## [1.0.0] - 2025-04-16

### Added
- Groups feature: group membership, member types, system groups (including an
  "Everybody" group), token-based joining, and group-based Lab authorization
  (#20, #35, #53).
- Answer sheet: show answers and add an answer sheet, with autosave for answers
  (#41, #50).
- Auto-redirect after login and current-running-labs count in the sidebar (#46,
  #48).
- Lab expiration with email alerts, scheduling and cleanup, plus an expiration
  extension UI (#127).
- Finished Labs page and sidebar link (#132).
- Multiple categories per Lab via a many-to-many relationship (#135).
- Hide/unhide feedback comments (#124).
- Account confirmation with email verification and token expiration handling
  (#78).
- Database migrations capability with a migrations folder (#62).
- Waiting-approval page for users without access; help and support page (#73).
- Customized register and error pages (403/404/500) with responsive design
  (#85, #90).
- Login with email or username (#97).
- Configurable mail SSL/TLS settings and dynamic analytics loader via env vars
  (#59, #86).
- Route access control via a user-category check decorator; auditing
  functionality with an AuditMixin (#11, #64).

### Fixed
- Fix authorization issues for SYSTEM groups and for students running/viewing
  labs (#81).
- Fix node IP assignment to match the actual pod, and list pods with node_ip
  (#42, #75).
- Various responsive-design fixes for images, tables and textareas (#56).
- Fix filters for showing running labs (#57).

## [1.0] - 2025-03-12

### Added
- Initial public release of the HackInSDN dashboard: Lab catalog and viewing,
  Lab guide editor with question buttons, save/resume Lab execution, answer
  saving, audit objects and functions, file/rotating logging with proxy IP
  support, and Docker-based deployment.
