# Plan: automatic grade/answers passback to the LMS (LTI AGS)

> **Status: implemented** — model/migration 2.0.13, `apps/lti/grades.py`,
> launch-context capture in `apps/lti/routes.py`, trigger in
> `view_finished_lab_infos`, tests in `tests/test_lti_grades.py`, user
> docs in DASHBOARD.md section 5.

When an LTI-launched user reaches `/finished-lab-infos/<lab_id>`, post
their lab answers back to the platform gradebook as a feedback comment,
and — when the lab has an answer sheet — a computed score, via LTI
Assignment and Grade Services (AGS).

Requirements mapping:

| # | Requirement | Where addressed |
|---|---|---|
| 1 | On finished-lab-infos, LTI users' answers are posted to the platform | trigger in `view_finished_lab_infos` + sender module |
| 2 | Answers dict → friendly Moodle comment | `build_answers_comment()` |
| 3 | No answers → still post grade/comment with question names, empty values | comment builder + score semantics below |
| 4 | Answer sheet registered → compute grade and send it | `compute_lab_score()` refactor + Grade payload |

## Existing pieces this builds on

- `view_finished_lab_infos` (apps/home/routes.py:1331) — renders the
  congratulations page; `lab_id` is the `Labs.id` (same key `LabAnswers`
  and `LabAnswerSheet` use).
- `LabAnswers` (per user+lab): `answers` JSON dict {question: answer},
  plus teacher-set `grades`/`comments` dicts.
- `LabAnswerSheet` (per lab): {question: expected-answer regex}.
- Scoring convention (apps/home/routes.py:list_lab_answers): questions =
  union(answer-sheet keys, manual-grade keys); a numeric manual grade
  contributes grade/100, otherwise full-match `re.match(f"^{expected}$",
  answer)` contributes 1; score = 100*correct/total.
- pylti1p3 AGS is usable **outside the launch request** — no launch cache
  needed: `ServiceConnector(registration, requests_session)` +
  `AssignmentsGradesService(connector, ags_endpoint_claim)`;
  `put_grade(grade)` posts to the claim's default `lineitem` URL.
  `Grade.get_value()` serializes only non-None fields, so a comment-only
  score (no `scoreGiven`) is valid — the LTI Score spec makes
  `scoreGiven` optional.

## Design

### 1. Persist the AGS context at launch (new model + migration 2.0.13)

The grade is sent long after the launch (hours later, possibly from
another worker), so the AGS claim must be persisted — the pylti1p3 launch
cache is the wrong tool (TTL, cache-local). New model in
`apps/lti/models.py`:

```python
class LtiLaunchContext(db.Model, AuditMixin):
    __tablename__ = "lti_launch_context"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True, nullable=False)
    issuer = db.Column(db.String(255), nullable=False)
    client_id = db.Column(db.String(255), nullable=False)
    deployment_id = db.Column(db.String(255))
    resource_link_id = db.Column(db.String(255))
    _ags = db.Column("ags", db.Text)          # JSON: the AGS endpoint claim
    custom_next_url = db.Column(db.String(255))  # for context->lab matching
    # unique (user_id, issuer, client_id, resource_link_id); updated_at from AuditMixin
```

In `launch()` (after `login_user`): read
`https://purl.imsglobal.org/spec/lti-ags/claim/endpoint` (skip storing if
absent — activity has no grade service), the `resource_link` claim id,
`deployment_id`, `client_id` from `aud` (string or first list element),
and the already-extracted custom `next_url`; upsert on the unique key.
One row per LMS activity the user launched, `updated_at` refreshed per
launch. Migration `2.0.12 → 2.0.13` creates the table.

**Context selection at send time** (a user may have launched several
activities): prefer the context whose `custom_next_url` points at the
finished lab (contains `/labs/<lab_id>` — pairs naturally with the
next_url deep-link feature, one LMS activity per lab); otherwise the most
recently `updated_at` context. This sends the grade to the right
gradebook column in multi-activity setups and degrades gracefully to
"latest activity" otherwise.

### 2. Request AGS scopes at dynamic registration

The `/lti/register/` payload currently sends `"scope": ""`. Change to:

```
https://purl.imsglobal.org/spec/lti-ags/scope/score
https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly
```

(space-separated). **Already-registered platforms** won't have the
service enabled: document that admins must either re-register or enable
"IMS LTI Assignment and Grade Services: use for grade sync" on the
existing Moodle tool entry by hand.

### 3. Score computation — shared helper (refactor)

Extract the scoring loop from `list_lab_answers` into
`compute_lab_score(answers, manual_grades, answer_sheet) ->
(score_pct | None, correct, total)` (None when there are no gradable
questions), in `apps/home` scope (e.g. `apps/utils.py`), and refactor
`list_lab_answers` to call it — same semantics, guarded by the existing
`tests/test_lab_answers.py`. The LTI sender reuses it so Dashboard and
LMS never disagree on the score.

### 4. Sender module — `apps/lti/grades.py`

```python
def build_answers_comment(lab_title, answers, answer_sheet, score_info) -> str
def send_lab_result_to_lms(user, lab) -> str|None   # status for logging/UI
```

Comment format (plain text — Moodle renders feedback as text), question
universe = union(answers keys, answer-sheet keys) so requirement 3 falls
out naturally:

```
Lab "Intro to SDN" completed on HackInSDN Dashboard.

Answers:
- q1_flows: 42
- q2_controller: (not answered)

Auto-grade from answer sheet: 66.67% (2 of 3 correct)
```

- user answered nothing but a sheet exists → every sheet question listed
  as `(not answered)`, and with the sheet present the computed score is
  0.0 — grade and comment still posted (requirement 3);
- no answers and no sheet → comment "No answers were submitted for this
  lab." and no score line.

`send_lab_result_to_lms`:

1. pick the `LtiLaunchContext` (see selection above); none → return
   ("not an LTI-launched user") — this is also the LTI-user test, no
   heuristics on `Users.issuer` needed;
2. claim must include the `score` scope and a `lineitem` URL, else log
   and skip (activity without a grade, or platform without AGS);
3. `DbToolConf().find_registration_by_params(issuer, client_id)` →
   `ServiceConnector(registration, get_requests_session())` →
   `AssignmentsGradesService(connector, ags_claim)`;
4. build `Grade`: `userId = user.subject` (the LTI sub), ISO timestamp,
   `activityProgress="Completed"`; with an answer sheet:
   `scoreGiven=<pct>`, `scoreMaximum=100`,
   `gradingProgress="FullyGraded"`; without: no score fields,
   `gradingProgress="PendingManual"` (comment-only; verify against a
   real Moodle in E2E — the spec allows it);
5. `ags.put_grade(grade)` → default lineitem.

Operational care:

- **The page must never break**: the trigger wraps the whole send in
  try/except, logs WARNING with issuer/user/lab on any failure, and the
  congratulations page renders regardless.
- **Timeouts**: this runs inline in the page request and
  `ServiceConnector` doesn't set one — use a `requests.Session` subclass
  with a default timeout (~10s) in `get_requests_session()` (benefits
  the token/keyset calls too).
- **Refresh = re-send**: one score per user per lineitem; a re-POST
  overwrites the same column (Moodle keeps grade history) — idempotent
  enough; no dedupe state needed initially.
- Teacher/admin launches: Moodle returns 400 for users without a
  gradable enrolment — log at INFO (known platform behavior, see
  PLAYBOOK.md field guide #4), don't alarm.

### 5. Trigger — `view_finished_lab_infos` (apps/home/routes.py)

```python
lti_grade_status = None
if app.config.get("ENABLE_LTI"):
    try:
        from apps.lti.grades import send_lab_result_to_lms  # lazy: optional module
        lti_grade_status = send_lab_result_to_lms(current_user, lab)
    except Exception:
        app.logger.warning(...)
return render_template(..., lti_grade_status=lti_grade_status)
```

Guarded + lazy import because `home` is a core blueprint and `lti` is
optional — the dashboard must keep working with the module disabled.
Optional UX nicety: an info box on the template ("Your results were sent
to your course gradebook") when the send succeeded.

## Tests

New `tests/test_lti_grades.py` (standard module header, "lg"-prefixed
seeds, registers the lti blueprint like tests/test_lti.py):

- **compute_lab_score** (unit): all-regex sheet; manual grade overriding
  regex; invalid regex ignored; unanswered questions count against the
  score; no sheet + no manual grades → `(None, 0, 0)`; refactored
  `list_lab_answers` regression covered by existing
  `tests/test_lab_answers.py` passing unmodified.
- **build_answers_comment** (unit): answered/unanswered mix; sheet-only
  names with `(not answered)` everywhere (requirement 3); no data at all;
  score line present only when a sheet exists.
- **Launch context persistence**: fake launch (existing
  `_FakeMessageLaunch` pattern) carrying an AGS endpoint claim → row
  created with claim/deployment/resource-link/custom next_url; re-launch
  updates the same row (no duplicates); launch without the claim → no
  row.
- **finished-lab-infos trigger** (AGS faked by monkeypatching
  `ServiceConnector`/`AssignmentsGradesService` in `apps.lti.grades` with
  a stub capturing `put_grade` payloads; parse `Grade.get_value()` JSON):
  - sheet + answers → correct `scoreGiven`, comment contains each
    question and answer;
  - sheet + **no** answers → `scoreGiven == 0.0`, comment lists all sheet
    question names as `(not answered)` (requirement 3);
  - answers + **no** sheet → payload has *no* `scoreGiven`,
    `gradingProgress == "PendingManual"`, comment present;
  - non-LTI user (no LtiLaunchContext) → no AGS call, page 200;
  - claim without `score` scope, or without `lineitem` → skipped +
    logged, page 200;
  - AGS stub raises (platform down) → page still 200, WARNING logged;
  - context selection: two contexts, one with `custom_next_url`
    matching the lab → its lineitem is used;
  - `OPTIONAL_MODULES` without `lti` → route untouched (guard test via
    `ENABLE_LTI=False` config toggle).
- **Registration scopes**: extend the existing dynamic-registration test
  to assert the AGS scopes in the posted `"scope"`.
- **E2E (manual, real Moodle)**: enable AGS grade sync on the tool,
  launch as an *enrolled student*, answer/finish a lab → gradebook shows
  the score and the feedback comment; repeat with no answers and with a
  lab lacking a sheet; confirm the teacher-launch 400 is logged politely.

## Docs

- `doc/lti/DASHBOARD.md`: new "Grade passback (AGS)" section — how it
  triggers, the answer-sheet requirement for scores, the
  re-register/enable-AGS note for pre-existing registrations, the
  one-activity-per-lab + `next_url` recommendation for correct gradebook
  columns.
- `doc/lti/IMPLEMENTATION_PLAN.md`: move AGS out of the out-of-scope
  list.

## Out of scope (future)

- Async/queued delivery with retry (inline best-effort is acceptable at
  current scale; revisit if platform latency hurts page loads).
- Creating extra lineitems (one column per lab under a single activity)
  — needs the full `lineitem` scope and column management; today one
  activity = one column.
- NRPS roster sync and submission-review launches.

## Rollout

Single PR: model + migration 2.0.13, registration scope change,
`compute_lab_score` refactor, `apps/lti/grades.py`, trigger, tests, docs.
After deploy: platforms registered before this feature need AGS enabled
(re-register or edit the Moodle tool); users must launch once more from
the LMS before their finishes can be graded (context is captured at
launch).
