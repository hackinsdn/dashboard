# -*- encoding: utf-8 -*-
"""HackInSDN"""

import json
import re
import calendar
from apps import db, cache
from apps.api import blueprint
from apps.controllers import k8s
from apps.home.models import Labs, LabInstances, LabAnswers, LabAnswerSheet, UserLikes, UserFeedbacks, lab_groups
from apps.authentication.models import Users, Groups, DeletedGroupUsers, group_members, group_owners
from flask import request, current_app, jsonify
from flask_login import login_required, current_user
from datetime import timedelta, datetime
from apps.utils import datetime_from_ts, parse_lab_expiration, shift_month_start, count_between
from sqlalchemy import func, extract
from math import floor

@blueprint.route('/pods/<lab_id>', methods=["GET"])
@login_required
def get_pods(lab_id):
    if current_user.category == "user":
        return {}, 404

    try:
        token = request.headers.get('Authorization').split()[1]
    except:
        return {"error": "invalid auth token"}, 400

    if not k8s.validate_token(token):
        return {"error": "Token not authorized"}, 404

    return k8s.get_pods_by_lab_id(lab_id), 200

@blueprint.route('/lab/status/<lab_id>', methods=["GET"])
@login_required
def get_lab_status(lab_id):
    if current_user.category == "user":
        return {}, 404

    lab = LabInstances.query.get(lab_id)
    if not lab:
        return {"status": "fail", "result": "Lab instance not found"}, 404
    
    if current_user.category == "student" and lab.user_id != current_user.id:
        return {"status": "fail", "result": "Unauthorized access to this lab"}, 401

    try:
        resources = k8s.get_resources_by_name(lab.k8s_resources)
    except Exception as exc:
        current_app.logger.error(f"Failed to obtain resource: {exc}")
        return {"status": "fail", "result": "Failed to obtain resource statuses"}, 400

    statuses = []
    for resource in resources:
        statuses.append("ok" if resource.get("is_ok") else "not-ok")
    return {"status": "ok", "result": statuses}, 200

@blueprint.route('/lab/<lab_id>', methods=["DELETE"])
@login_required
def delete_lab(lab_id):
    if current_user.category == "user":
        return {}, 404

    lab = LabInstances.query.get(lab_id)
    if not lab:
        return {"status": "fail", "result": "Lab instance not found"}, 404

    if current_user.category != "admin" and lab.user_id != current_user.id:
        return {"status": "fail", "result": "Unauthorized access to this lab"}, 401

    try:
        results = k8s.delete_resources_by_name(lab.k8s_resources)
    except Exception as exc:
        current_app.logger.error(f"Failed to delete resources: {exc}")
        return {"status": "fail", "result": "Failed to delete resources"}, 400

    who = "owner" if lab.user_id == current_user.id else "admin"
    lab.is_deleted = True
    lab.finish_reason = "Finished by the " + who
    db.session.commit()

    running_labs = LabInstances.query.filter_by(is_deleted=False, user_id=current_user.id).count()
    cache.set(f"running_labs-{current_user.id}", running_labs)

    if sum(results) == len(lab.k8s_resources):
        return {"status": "ok", "result": "Resources removed successfully!"}, 200

    msg = "Some resources failed to be removed: "
    for idx, resource in enumerate(lab.k8s_resources):
        status = "ok" if results[idx] else "fail"
        msg += f"{resource['kind']}/{resource['name']}={status}; "
    return {"status": "ok", "result": msg}, 200

@blueprint.route('/nodes', methods=["GET"])
@login_required
def get_nodes():
    if current_user.category == "user":
        return {}, 404

    try:
        k8s_nodes = k8s.get_nodes()
    except Exception as exc:
        current_app.logger.error(f"Failed to obtain nodes: {exc}")
        return {"status": "fail", "result": "Failed to obtain resource statuses"}, 400

    nodes = {}
    for node in k8s_nodes:
        nodes[node["name"]] = {
            "latitude": node["latitude"],
            "longitude": node["longitude"],
            "tooltip": f"Node: {node['name']} | Status: {node['status']}",
            "value": "unknown value",
        }
    return {"status": "ok", "result": nodes}, 200

@blueprint.route('/users/bulk-approve', methods=["POST"])
@login_required
def bulk_approve_users():
    if current_user.category not in ["admin", "teacher"]:
        return {}, 401

    content = request.get_json(silent=True)
    if not content:
        return {"status": "fail", "result": "invalid content"}, 400

    users = []
    errors = []
    for user_id in content:
        if not user_id.isdigit():
            errors.append(f"Invalid user provided {user_id=}")
            continue
        user = Users.query.get(int(user_id))
        if not user or user.is_deleted or user.category != "user":
            errors.append(f"Invalid user provided {user_id=}")
            continue
        users.append(user)

    if errors:
        return {"status": "fail", "result": "Invalid users to approve: " + "<br/>".join(errors)}, 400

    for user in users:
        user.category = "student"

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to approve users: {exc}")
        return {"status": "fail", "result": "Failed to save updated data"}, 400

    return {"status": "ok", "result": "all users approved"}, 200

@blueprint.route('/lab_answers/<lab_inst_id>', methods=["GET"])
@login_required
def get_lab_answers(lab_inst_id):
    if current_user.category == "user":
        return {}, 404

    lab_inst = LabInstances.query.get(lab_inst_id)
    if not lab_inst:
        return {"status": "fail", "result": "Lab instance not found"}, 404

    if lab_inst.user_id != current_user.id:
        return {"status": "fail", "result": "Unauthorized access to this lab"}, 401

    answers = {}
    lab_answers = LabAnswers.query.filter_by(lab_id=lab_inst.lab_id, user_id=current_user.id).first()
    if lab_answers:
        answers = json.loads(lab_answers.answers)

    return {"status": "ok", "result": answers}, 200

@blueprint.route('/lab_answers/<lab_inst_id>', methods=["POST"])
@login_required
def save_lab_answers(lab_inst_id):
    if current_user.category == "user":
        return {}, 404

    content = request.get_json(silent=True)
    if not content:
        return {"status": "fail", "result": "invalid content"}, 400

    lab_inst = LabInstances.query.get(lab_inst_id)
    if not lab_inst:
        return {"status": "fail", "result": "Lab instance not found"}, 404

    if lab_inst.user_id != current_user.id:
        return {"status": "fail", "result": "Unauthorized access to this lab"}, 401

    lab_answers = LabAnswers.query.filter_by(lab_id=lab_inst.lab_id, user_id=current_user.id).first()
    if not lab_answers:
        lab_answers = LabAnswers(user_id=current_user.id, lab_id=lab_inst.lab_id)
        db.session.add(lab_answers)

    lab_answers.answers = json.dumps(content)
    db.session.commit()

    return {"status": "ok", "result": "Answers saved successfully"}, 200

@blueprint.route('/lab_answers/grades_comments/<int:answer_id>', methods=["POST"])
@login_required
def save_grades_comments(answer_id):
    if current_user.category not in ["admin", "teacher"]:
        return {"status": "fail", "result": "User not authorized"}, 401

    lab_answers = LabAnswers.query.get(answer_id)
    if not lab_answers:
        return {"status": "fail", "result": "Lab answers not found"}, 404

    # Check for authorization: is this user a teacher who is owner of this group?
    query = db.session.query(LabAnswers).filter(
        LabAnswers.id==answer_id
    ).join(lab_groups, LabAnswers.lab_id == lab_groups.c.lab_id).join(
        group_owners, lab_groups.c.group_id == group_owners.c.group_id
    ).join(
        group_members,
        group_members.c.group_id == lab_groups.c.group_id and LabAnswers.user_id == group_members.c.user_id
    ).filter(group_owners.c.user_id == current_user.id).first()

    if current_user.category != "admin" and not query:
        return {"status": "fail", "result": "Not authorized to save answers"}, 401

    data = request.get_json(silent=True)
    if not data:
        return {"status": "fail", "result": "invalid content"}, 400

    for k, v in data.get('grades', {}).items():
        if not v:
            continue
        if not isinstance(v, (int, float)) or v < 0 or v > 100:
            return {"status": "fail", "result": f"Invalid grade for question {k}"}, 400

    lab_answers.comments = json.dumps(data.get('comments', {}))
    lab_answers.grades = json.dumps(data.get('grades', {}))

    db.session.commit()
    
    return {"status": "ok", "result": "Answers saved successfully"}, 200

@blueprint.route('/users/<int:user_id>', methods=["DELETE"])
@login_required
def delete_user(user_id):
    if current_user.category != "admin":
        return {"status": "fail", "result": "Unauthorized access"}, 401

    user = Users.query.get(user_id)
    if not user or user.is_deleted:
        return {"status": "fail", "result": "User not found"}, 404

    labs = LabInstances.query.filter_by(user_id=user_id, is_deleted=False)
    if labs.count() > 0:
        return {"status": "fail", "result": "Failed to delete user: user has labs running"}, 400

    user.is_deleted = True
    deleted = DeletedGroupUsers()
    deleted.object_id = user.id
    deleted.object_type = "user"
    deleted.members = json.dumps([group.id for group in user.member_of_groups])
    deleted.assistants = json.dumps([group.id for group in user.assistant_of_groups])
    deleted.owners = json.dumps([group.id for group in user.owner_of_groups])
    db.session.add(deleted)
    user.member_of_groups.clear()
    user.assistant_of_groups.clear()
    user.owner_of_groups.clear()

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to delete user {user_id}: {exc}")
        return {"status": "fail", "result": "Failed to delete user"}, 400

    return {"status": "ok", "result": "User deleted successfully"}, 200


@blueprint.route('/groups/<int:group_id>', methods=["DELETE"])
@login_required
def delete_group(group_id):
    if current_user.category not in ["admin", "teacher"]:
        return {"status": "fail", "result": "Unauthorized access"}, 401

    group = Groups.query.get(group_id)
    if not group or group.is_deleted:
        return {"status": "fail", "result": "Group not found"}, 404

    if group.organization == "SYSTEM" and current_user.category != "admin":
        return {"status": "fail", "result": "Only admins can change System groups"}, 404

    if current_user.category == "teacher" and not group.is_owner(current_user.id):
        return {"status": "fail", "result": "Unauthorized access to this group"}, 401

    group.is_deleted = True
    deleted = DeletedGroupUsers()
    deleted.object_id = group.id
    deleted.object_type = "group"
    deleted.members = json.dumps(group.members_dict.keys())
    deleted.assistants = json.dumps(group.assistants_dict.keys())
    deleted.owners = json.dumps(group.owners_dict.keys())
    db.session.add(deleted)
    group.members.clear()
    group.assistants.clear()
    group.owners.clear()

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to delete group {group_id}: {exc}")
        return {"status": "fail", "result": "Failed to delete group"}, 400

    return {"status": "ok", "result": "Group deleted successfully"}, 200

@blueprint.route('/groups/join/<int:group_id>', methods=["POST"])
@login_required
def join_group(group_id):
    content = request.get_json(silent=True)
    if not content or not content.get("accessToken"):
        return {"status": "fail", "result": "invalid content"}, 400

    group = Groups.query.get(group_id)
    if not group or group.is_deleted:
        return {"status": "fail", "result": "Group not found"}, 404

    if group.organization == "SYSTEM" and current_user.category != "admin":
        return {"status": "fail", "result": "Only admins can change System groups"}, 404

    if not group.accesstoken or group.accesstoken != content.get("accessToken"):
        return {"status": "fail", "result": "Invalid group access token"}, 400

    if group.is_member(current_user.id):
        return {"status": "ok", "result": "Already member of group"}, 200

    group.members.append(current_user)

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to join group {group_id}: {exc}")
        return {"status": "fail", "result": "Failed to join group"}, 400

    return {"status": "ok", "result": "Joint group successfully! Click on 'Reload profile' to update your authorization."}, 200

@blueprint.route('/lab_answers/check/<lab_id>/<int:answer_id>')
@login_required
def check_lab_answer(lab_id, answer_id):
    if current_user.category not in ["admin", "teacher"]:
        return {"status": "fail", "result": "Unauthorized access"}, 401

    lab = Labs.query.get(lab_id)
    if not lab:
        return {"status": "fail", "result": "Invalid or Unauthorized access to lab"}, 401

    mygroups = current_user.privileged_group_ids
    for group in lab.allowed_groups:
        if group.id in mygroups:
            break
    else:
        return {"status": "fail", "result": "Invalid or Unauthorized access to lab"}, 401

    lab_answer = LabAnswers.query.get(answer_id)
    if not lab_answer:
        return {"status": "fail", "result": "Invalid or Unauthorized access to lab answer"}, 401

    lab_answer_sheet = LabAnswerSheet.query.filter_by(lab_id=lab_id).first()
    if not lab_answer_sheet:
        return {"status": "fail", "result": "No Lab Answer Sheet available. Please create the Answer Sheet first."}, 400

    questions = set()
    answer_sheet = lab_answer_sheet.answers_dict
    answers = lab_answer.answers_dict
    grades = lab_answer.grades_dict
    questions.update(answer_sheet)
    questions.update(grades)
    total, correct = 0, 0
    for question in questions:
        total += 1
        grade_value = grades.get(question)
        if isinstance(grade_value, (int, float)):
            correct += float(grade_value) / 100
            continue
        if not (expected_answer := answer_sheet.get(question)):
            continue
        try:
            if re.match(fr"^{expected_answer}$", answers.get(question)):
                correct += 1
        except:
            continue
    score = "%.2f" % (100*correct/total) if total > 0 else "--"

    return {"status": "ok", "result": score}, 200


@blueprint.route('/feedback', methods=["POST", "GET"])
@login_required
def feedback():
    if current_user.category == "user":
        return {"status": "fail", "result": "Unauthorized access"}, 401

    user_feedbacks = cache.get("user_feedbacks")
    if user_feedbacks is None:
        user_feedbacks = UserFeedbacks.query.filter_by(is_hidden=False).order_by(UserFeedbacks.created_at.desc()).limit(5).all()
        user_feedbacks = [fb.as_dict() for fb in user_feedbacks]
        cache.set("user_feedbacks", user_feedbacks)

    if request.method == "GET":
        return {"status": "ok", "recent_feedbacks": user_feedbacks}, 200

    data = request.get_json()
    stars = data.get("rating")
    comment = data.get("comment", "")

    if not stars:
        return {"status": "fail", "result": "Stars mandatory"}, 400

    existing_feedback = UserFeedbacks.query.filter_by(user_id=current_user.id).first()
    if existing_feedback:
        return {"status": "fail", "result": "Feedback already given!"}, 400

    try:
        new_feedback = UserFeedbacks(
            user_id=current_user.id,
            stars=stars,
            comment=comment
        )
        db.session.add(new_feedback)
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to save user feedback for {current_user.id}: {exc}")
        return {"status": "fail", "result": "Failed to save user feedback"}, 400

    user_feedbacks.insert(0, new_feedback.as_dict())
    if len(user_feedbacks) > 5:
        user_feedbacks.pop(-1)
    cache.set("user_feedbacks", user_feedbacks)

    return {
        "status": "ok",
        "result": "Feedback given successfully",
        "recent_feedbacks": user_feedbacks,
    }, 200


@blueprint.route('/user_like', methods=["POST"])
@login_required
def add_user_like():
    counter = cache.get("user_likes") or UserLikes.query.count()
    user_like = UserLikes.query.get(current_user.id)
    if user_like:
        return {"status": "ok", "result": counter}, 200
    user_like = UserLikes(user_id=current_user.id)
    db.session.add(user_like)
    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to add user like: {exc}")
        return {"status": "fail", "result": "Failed to add user like"}, 400
    cache.set("user_likes", counter+1)
    return {"status": "ok", "result": counter+1}, 200


@blueprint.route('/user_like', methods=["DELETE"])
@login_required
def del_user_like():
    db.session.query(UserLikes).filter(UserLikes.user_id == current_user.id).delete()
    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to delete user like: {exc}")
        return {"status": "fail", "result": "Failed to delete user like"}, 400
    counter = cache.get("user_likes") or UserLikes.query.count()
    counter = max(counter-1, 0)
    cache.set("user_likes", counter)
    return {"status": "ok", "result": counter}, 200

@blueprint.route('/lab/<lab_id>/extend', methods=["POST"])
@login_required
def extend_lab(lab_id):

    lab_instance = LabInstances.query.get(lab_id)
    if not lab_instance:
        return {"status": "fail", "result": "Lab instance not found"}, 404

    if current_user.category != "admin" and lab_instance.user_id != current_user.id:
        return {"status": "fail", "result": "Unauthorized access to this lab"}, 401

    content = request.get_json(silent=True)
    if not content or 'extend_hours' not in content:
        return {"status": "fail", "result": "Invalid content"}, 400

    extend_hours = content['extend_hours']
    if not isinstance(extend_hours, int) or extend_hours <= 0 or extend_hours > 720:
        return {"status": "fail", "result": "Invalid extend hours"}, 400

    # Logic to extend the lab instance scheduling
    try:
        expiration_ts = parse_lab_expiration(extend_hours)
        new_expiration = datetime_from_ts(expiration_ts)
        lab_instance.expiration_ts = expiration_ts
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(
            "Failed to extend lab duration/expiration "
            f"{lab_id=} {current_user.id=} {extend_hours=}: {exc}"
        )
        return {"status": "fail", "result": f"Failed updating lab duration, please contact the administrator."}, 400

    current_app.logger.error(
        "Lab duration/expiration extended successfully: "
            f"{lab_id=} {current_user.id=} {new_expiration=}"
    )

    return {"status": "ok", "result": new_expiration}, 200


@blueprint.route("/lab/usage_stats", methods=["GET"])
@login_required
def get_lab_usage_stats():
    if cached := cache.get("lab_usage_stats"):
        return cached

    today = datetime.now()

    month_keys = [(d.year, d.month) for d in (shift_month_start(today, i) for i in range(-5, 1))]
    months = [calendar.month_name[m] for _, m in month_keys]

    six_months_ago = shift_month_start(today, -5)
    one_month_ago = shift_month_start(today, 0)
    two_months_ago = shift_month_start(today, -1)
    current_month = shift_month_start(today, 1)

    # TODO: Refactor to reduce the amount of queries
    completed_labs_from_last_six_months = count_between(LabInstances, LabInstances.created_at, six_months_ago, current_month, [LabInstances.is_deleted.is_(True)])
    completed_labs_from_current_month = count_between(LabInstances, LabInstances.created_at, one_month_ago, current_month, [LabInstances.is_deleted.is_(True)])
    completed_labs_from_last_month = count_between(LabInstances, LabInstances.created_at, two_months_ago, one_month_ago, [LabInstances.is_deleted.is_(True)])
    
    # TODO: Refactor to reduce the amount of queries
    completed_challenges_from_last_six_months = count_between(LabAnswers, LabAnswers.created_at, six_months_ago, current_month)
    completed_challenges_from_current_month = count_between(LabAnswers, LabAnswers.created_at, one_month_ago, current_month)
    completed_challenges_from_last_month = count_between(LabAnswers, LabAnswers.created_at, two_months_ago, one_month_ago)

    answers = (
        LabAnswers.query
        .filter(LabAnswers.created_at >= six_months_ago, LabAnswers.created_at <= current_month)
        .all()
    )
    answered_questions_from_last_six_months = sum(len(a.answers_dict) for a in answers)
    answered_questions_from_current_month = sum(len(a.answers_dict) for a in answers if a.created_at >= one_month_ago)
    answered_questions_from_last_month = sum(len(a.answers_dict) for a in answers if a.created_at >= two_months_ago and a.created_at <= one_month_ago)

    # TODO: Refactor to reduce the amount of queries
    lab_instances_executed_from_last_six_months_counts = {
        (int(y), int(m)): c
        for y, m, c in db.session.query(
            func.extract('year', LabInstances.created_at),
            func.extract('month', LabInstances.created_at),
            func.count()
        )
        .filter(LabInstances.created_at >= six_months_ago, LabInstances.created_at <= current_month)
        .group_by(func.extract('year', LabInstances.created_at), func.extract('month', LabInstances.created_at))
        .all()
    }
    lab_instances_executed_from_last_six_months_data = [lab_instances_executed_from_last_six_months_counts.get(k, 0) for k in month_keys]

    completed_labs_growth = completed_labs_from_current_month - completed_labs_from_last_month
    completed_challenges_growth = completed_challenges_from_current_month - completed_challenges_from_last_month
    answered_questions_growth = answered_questions_from_current_month - answered_questions_from_last_month

    data = {
        "start_date": six_months_ago.strftime("%#d %b, %Y"),
        "end_date": today.strftime("%#d %b, %Y"),
        "months": months,
        "labs_executed_counts": lab_instances_executed_from_last_six_months_data,
        "completed_labs_from_last_six_months": completed_labs_from_last_six_months,
        "completed_labs_from_current_month": completed_labs_from_current_month,
        "completed_challenges_from_last_six_months": completed_challenges_from_last_six_months,
        "completed_challenges_from_current_month": completed_challenges_from_current_month,
        "answered_questions_from_last_six_months": answered_questions_from_last_six_months,
        "answered_questions_from_current_month": answered_questions_from_current_month,
        "completed_labs_growth": completed_labs_growth,
        "completed_challenges_growth": completed_challenges_growth,
        "answered_questions_growth": answered_questions_growth,
    }

    cache.set("lab_usage_stats", data)
    return data
