# -*- encoding: utf-8 -*-
"""AGS grade passback: when an LTI-launched user finishes a lab, post their
answers to the platform gradebook as a feedback comment and - when the lab
has an answer sheet - a computed score. Works entirely from the persisted
LtiLaunchContext (no pylti1p3 launch cache involved), so it survives worker
changes and long-running labs."""

from flask import current_app as app
from pylti1p3.assignments_grades import AssignmentsGradesService
from pylti1p3.exception import LtiServiceException
from pylti1p3.grade import Grade
from pylti1p3.lineitem import LineItem
from pylti1p3.service_connector import ServiceConnector

from apps.audit_mixin import utcnow
from apps.home.models import LabAnswers, LabAnswerSheet
from apps.lti.models import LtiLaunchContext
from apps.lti.routes import get_requests_session
from apps.lti.tool_conf import DbToolConf
from apps.utils import compute_lab_score

AGS_SCORE_SCOPE = "https://purl.imsglobal.org/spec/lti-ags/scope/score"


def pick_launch_context(user, lab_id):
    """The launch context whose grade column should receive this lab's
    result: prefer a context whose custom next_url deep-links to this lab
    (one LMS activity per lab setup), else the most recent launch."""
    contexts = (
        LtiLaunchContext.query.filter_by(user_id=user.id)
        .order_by(LtiLaunchContext.updated_at.desc())
        .all()
    )
    for context in contexts:
        if context.custom_next_url and str(lab_id) in context.custom_next_url:
            return context
    return contexts[0] if contexts else None


def build_answers_comment(lab_title, answers, answer_sheet, score_info):
    """Friendly plain-text gradebook comment. The question universe is the
    union of the user's answers and the answer sheet, so a user who answered
    nothing still gets every known question listed as '(not answered)'."""
    answers = answers or {}
    answer_sheet = answer_sheet or {}
    questions = sorted(set(answers) | set(answer_sheet))
    lines = [f'Lab "{lab_title}" completed on HackInSDN Dashboard.', ""]
    if not questions:
        lines.append("No answers were submitted for this lab.")
    else:
        lines.append("Answers:")
        for question in questions:
            value = answers.get(question)
            if value in (None, ""):
                value = "(not answered)"
            lines.append(f"- {question}: {value}")
    score, correct, total = score_info
    if score is not None:
        lines.append("")
        lines.append(
            f"Auto-grade from answer sheet: {score:.2f}% ({correct:g} of {total} correct)"
        )
    return "\n<br/>".join(lines)


def send_lab_result_to_lms(user, lab):
    """Post the user's answers (comment) and score (when an answer sheet is
    registered) to the LTI platform's gradebook. Returns a short status
    string for logging/UI, or None when the user has no LTI launch context
    (i.e. not an LTI user). Expected platform-side conditions are logged and
    reported, never raised."""
    context = pick_launch_context(user, lab.id)
    if not context:
        return None

    log_ref = f"user={user.username} lab={lab.id} issuer={context.issuer}"
    ags_claim = context.ags
    if AGS_SCORE_SCOPE not in (ags_claim.get("scope") or []):
        app.logger.info(f"LTI grade passback skipped (no score scope) {log_ref}")
        return "skipped: platform did not grant the AGS score scope"
    if not ags_claim.get("lineitem") and not ags_claim.get("lineitems"):
        app.logger.info(f"LTI grade passback skipped (no lineitem) {log_ref}")
        return "skipped: LMS activity has no gradebook line item"

    lab_answers = LabAnswers.query.filter_by(lab_id=lab.id, user_id=user.id).first()
    answers = lab_answers.answers_dict if lab_answers else {}
    manual_grades = lab_answers.grades_dict if lab_answers else {}
    sheet_row = LabAnswerSheet.query.filter_by(lab_id=lab.id).first()
    answer_sheet = sheet_row.answers_dict if sheet_row else {}

    # a grade is only computed when the lab has an answer sheet registered;
    # without one the score is left for the teacher (comment-only post)
    if answer_sheet:
        score, correct, total = compute_lab_score(answers, manual_grades, answer_sheet)
    else:
        score, correct, total = None, 0, 0

    comment = build_answers_comment(lab.title, answers, answer_sheet, (score, correct, total))
    grade = Grade()
    grade.set_user_id(user.subject)
    grade.set_timestamp(utcnow().isoformat())
    grade.set_activity_progress("Completed")
    grade.set_comment(comment)
    if score is not None:
        grade.set_score_given(score).set_score_maximum(100)
        grade.set_grading_progress("FullyGraded")
    else:
        grade.set_grading_progress("PendingManual")

    registration = DbToolConf().find_registration_by_params(context.issuer, context.client_id)
    connector = ServiceConnector(registration, get_requests_session())
    ags = AssignmentsGradesService(connector, ags_claim)
    try:
        lineitem = None
        if not ags_claim.get("lineitem"):
            lineitem = _resolve_lineitem(ags, context, lab)
            if lineitem is None:
                app.logger.info(
                    f"LTI grade passback skipped (no lineitem resolved) {log_ref} "
                    f"ags_claim={ags_claim}"
                )
                return "skipped: no gradebook line item could be resolved for this activity"
        if lineitem:
            ags.put_grade(grade, lineitem)
        else:
            ags.put_grade(grade)
    except LtiServiceException as exc:
        # a 400 here is usually Moodle refusing scores for users without a
        # gradable enrolment (teacher/admin launches) - known behavior
        app.logger.info(f"LTI grade passback rejected by platform {log_ref}: {exc}")
        return "rejected by the LMS (user may not have a gradable enrolment)"

    app.logger.info(f"LTI grade passback sent {log_ref} score={score}")
    return "sent"


def _resolve_lineitem(ags, context, lab):
    """Moodle only sends a default `lineitem` claim when the activity has a
    coupled gradebook column; with plain "grade sync" (no column
    management) or course-level tools only the `lineitems` collection URL
    arrives. Locate the activity's column there by resource link, or create
    one when the platform granted the full lineitem scope."""
    if not ags.can_read_lineitem():
        return None
    lineitems = ags.get_lineitems()
    if context.resource_link_id:
        for item in lineitems:
            if item.get("resourceLinkId") == context.resource_link_id:
                return LineItem(dict(item))
    if ags.can_create_lineitem():
        new_lineitem = LineItem()
        new_lineitem.set_tag(f"hackinsdn-lab-{lab.id}")
        new_lineitem.set_label(lab.title or f"Lab {lab.id}")
        new_lineitem.set_score_maximum(100)
        if context.resource_link_id:
            new_lineitem.set_resource_link_id(context.resource_link_id)
        return ags.find_or_create_lineitem(new_lineitem)
    # distinguish "activity has no gradebook column at all" (empty
    # collection: set a Grade on the Moodle activity) from a resource-link
    # mismatch - this log line is the difference
    app.logger.info(
        f"LTI lineitems collection: {len(lineitems)} item(s), none matching "
        f"resource_link_id={context.resource_link_id}, no create scope; "
        f"items={[(i.get('id'), i.get('resourceLinkId'), i.get('label')) for i in lineitems[:10]]}"
    )
    return None
