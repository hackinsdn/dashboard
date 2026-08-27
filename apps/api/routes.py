# -*- encoding: utf-8 -*-
"""HackInSDN"""

import json
import re
from apps import db, cache
from apps.api import blueprint
from apps.controllers import k8s, git
from apps.controllers import support
from apps.controllers import rag_client
from apps.controllers import lab_versions
from apps.home.models import Labs, LabInstances, LabAnswers, LabAnswerSheet, UserLikes, UserFeedbacks, lab_groups, LabCategories, SupportThreads, SupportMessages
from apps.authentication.models import Users, Groups, DeletedGroupUsers, group_members, group_owners
from apps.audit_mixin import check_user_category, get_remote_addr
from flask import request, current_app
from flask_babel import gettext as _, get_locale
from flask_login import login_required, current_user
from datetime import timedelta, datetime
from apps.utils import datetime_from_ts, parse_lab_expiration, check_pre_approved, secure_filename


@blueprint.route('/pods/<lab_id>', methods=["GET"])
@login_required
def get_pods(lab_id):
    if current_user.category == "user":
        return {}, 404

    try:
        token = request.headers.get('Authorization').split()[1]
    except:
        return {"error": _("invalid auth token")}, 400

    if not k8s.validate_token(token):
        return {"error": _("Token not authorized")}, 404

    return k8s.get_pods_by_lab_id(lab_id), 200

@blueprint.route('/lab/status/<lab_id>', methods=["GET"])
@login_required
def get_lab_status(lab_id):
    if current_user.category == "user":
        return {}, 404

    lab = db.session.get(LabInstances, lab_id)
    if not lab:
        return {"status": "fail", "result": _("Lab instance not found")}, 404
    
    if lab.user_id != current_user.id:
        return {"status": "fail", "result": _("Unauthorized access to this lab")}, 401

    try:
        resources = k8s.get_resources_by_name(lab.k8s_resources)
    except Exception as exc:
        current_app.logger.error(f"Failed to obtain resource: {exc}")
        return {"status": "fail", "result": _("Failed to obtain resource statuses")}, 400

    statuses = []
    for resource in resources:
        statuses.append({
            "name": f"{resource['kind']}__{resource['metadata']['name']}",
            "status": "ok" if resource.get("is_ok") else "not-ok",
        })
    return {"status": "ok", "result": statuses}, 200

@blueprint.route('/lab/<lab_id>', methods=["DELETE"])
@login_required
def delete_lab(lab_id):
    if current_user.category == "user":
        return {}, 404

    lab = db.session.get(LabInstances, lab_id)
    if not lab:
        return {"status": "fail", "result": _("Lab instance not found")}, 404

    if current_user.category != "admin" and lab.user_id != current_user.id:
        return {"status": "fail", "result": _("Unauthorized access to this lab")}, 401

    try:
        results = k8s.delete_resources_by_name(lab.k8s_resources)
    except Exception as exc:
        current_app.logger.error(f"Failed to delete resources: {exc}")
        return {"status": "fail", "result": _("Failed to delete resources")}, 400

    if sum(results) != len(lab.k8s_resources):
        # some resources are still present: keep the instance (do NOT mark it
        # deleted) so it can be retried, instead of orphaning them in the
        # cluster with no DB record to track them.
        msg = "Some resources failed to be removed: "
        for idx, resource in enumerate(lab.k8s_resources):
            status = "ok" if results[idx] else "fail"
            msg += f"{resource['kind']}/{resource['name']}={status}; "
        return {"status": "fail", "result": msg}, 400

    who = "owner" if lab.user_id == current_user.id else "admin"
    lab.is_deleted = True
    lab.finish_reason = "Finished by the " + who
    db.session.commit()

    running_labs = LabInstances.query.filter_by(is_deleted=False, user_id=current_user.id).count()
    cache.set(f"running_labs-{current_user.id}", running_labs)

    return {"status": "ok", "result": _("Resources removed successfully!")}, 200

@blueprint.route('/labs', methods=["DELETE"])
@login_required
def delete_labs():
    if current_user.category == "user":
        return {}, 404

    content = request.get_json(silent=True)
    if not content or not isinstance(content, list):
        return {"status": "fail", "result": _("invalid content")}, 400

    labs = []
    errors = []
    for lab_id in content:
        lab = db.session.get(LabInstances, lab_id) if isinstance(lab_id, str) else None
        if not lab or lab.is_deleted:
            errors.append(f"Lab instance not found {lab_id=}")
            continue
        if current_user.category != "admin" and lab.user_id != current_user.id:
            errors.append(f"Unauthorized access to this lab {lab_id=}")
            continue
        labs.append(lab)

    if errors:
        return {"status": "fail", "result": _("Invalid labs to delete:") + " " + "<br/>".join(errors)}, 400

    failed = []
    partial = []
    deleted = []
    for lab in labs:
        try:
            results = k8s.delete_resources_by_name(lab.k8s_resources)
        except Exception as exc:
            current_app.logger.error(f"Failed to delete resources of lab {lab.id}: {exc}")
            failed.append(lab.id)
            continue

        if sum(results) != len(lab.k8s_resources):
            # some resources are still present: keep the instance (do NOT mark
            # it deleted) so it can be retried, instead of orphaning them in
            # the cluster with no DB record to track them.
            for idx, resource in enumerate(lab.k8s_resources):
                if not results[idx]:
                    partial.append(f"{lab.id}: {resource['kind']}/{resource['name']}")
            continue

        who = "owner" if lab.user_id == current_user.id else "admin"
        lab.is_deleted = True
        lab.finish_reason = "Finished by the " + who
        deleted.append(lab)

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to delete labs: {exc}")
        return {"status": "fail", "result": _("Failed to save updated data")}, 400

    for user_id in {lab.user_id for lab in deleted}:
        running_labs = LabInstances.query.filter_by(is_deleted=False, user_id=user_id).count()
        cache.set(f"running_labs-{user_id}", running_labs)

    if failed or partial:
        problems = []
        if failed:
            problems.append(_("Failed to delete resources") + ": " + "; ".join(failed))
        if partial:
            problems.append("Some resources failed to be removed: " + "; ".join(partial))
        return {"status": "fail", "result": "<br/>".join(problems)}, 400

    return {"status": "ok", "result": _("Resources removed successfully!")}, 200

@blueprint.route('/nodes', methods=["GET"])
@login_required
def get_nodes():
    if current_user.category == "user":
        return {}, 404

    try:
        k8s_nodes = k8s.get_nodes()
    except Exception as exc:
        current_app.logger.error(f"Failed to obtain nodes: {exc}")
        return {"status": "fail", "result": _("Failed to obtain resource statuses")}, 400

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
        return {"status": "fail", "result": _("invalid content")}, 400

    users = []
    errors = []
    for user_id in content:
        if not user_id.isdigit():
            errors.append(f"Invalid user provided {user_id=}")
            continue
        user = db.session.get(Users, int(user_id))
        if not user or user.is_deleted or user.category != "user":
            errors.append(f"Invalid user provided {user_id=}")
            continue
        users.append(user)

    if errors:
        return {"status": "fail", "result": _("Invalid users to approve:") + " " + "<br/>".join(errors)}, 400

    for user in users:
        user.category = "student"

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to approve users: {exc}")
        return {"status": "fail", "result": _("Failed to save updated data")}, 400

    return {"status": "ok", "result": _("all users approved")}, 200

@blueprint.route('/lab_answers/<lab_inst_id>', methods=["GET"])
@login_required
def get_lab_answers(lab_inst_id):
    if current_user.category == "user":
        return {}, 404

    lab_inst = db.session.get(LabInstances, lab_inst_id)
    if not lab_inst:
        return {"status": "fail", "result": _("Lab instance not found")}, 404

    if lab_inst.user_id != current_user.id:
        return {"status": "fail", "result": _("Unauthorized access to this lab")}, 401

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
    if not isinstance(content, dict) or not content:
        return {"status": "fail", "result": _("invalid content")}, 400

    lab_inst = db.session.get(LabInstances, lab_inst_id)
    if not lab_inst:
        return {"status": "fail", "result": _("Lab instance not found")}, 404

    if lab_inst.user_id != current_user.id:
        return {"status": "fail", "result": _("Unauthorized access to this lab")}, 401

    lab_answers = LabAnswers.query.filter_by(lab_id=lab_inst.lab_id, user_id=current_user.id).first()
    if not lab_answers:
        lab_answers = LabAnswers(user_id=current_user.id, lab_id=lab_inst.lab_id)
        db.session.add(lab_answers)

    current_app.logger.info(
        f"Save lab_answers id={lab_answers.id} user={current_user.username} "
        f"lab={lab_inst.lab_id} request-answers={content}"
    )

    # Merge into the stored answers rather than replacing them: keep keys not
    # present in this payload, and don't let an empty incoming value wipe an
    # answer that was already saved (guards against a partial/early auto-save
    # clobbering existing work).
    merged = lab_answers.answers_dict
    for name, value in content.items():
        if (value is None or value == "") and merged.get(name):
            continue
        merged[name] = value

    lab_answers.answers = json.dumps(merged)
    db.session.commit()

    return {"status": "ok", "result": _("Answers saved successfully")}, 200

@blueprint.route('/lab_answers/grades_comments/<int:answer_id>', methods=["POST"])
@login_required
def save_grades_comments(answer_id):
    if current_user.category not in ["admin", "teacher"]:
        return {"status": "fail", "result": _("User not authorized")}, 401

    lab_answers = db.session.get(LabAnswers, answer_id)
    if not lab_answers:
        return {"status": "fail", "result": _("Lab answers not found")}, 404

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
        return {"status": "fail", "result": _("Not authorized to save answers")}, 401

    data = request.get_json(silent=True)
    if not data:
        return {"status": "fail", "result": _("invalid content")}, 400

    for k, v in data.get('grades', {}).items():
        if not v:
            continue
        if not isinstance(v, (int, float)) or v < 0 or v > 100:
            return {"status": "fail", "result": _("Invalid grade for question %(q)s", q=k)}, 400

    lab_answers.comments = json.dumps(data.get('comments', {}))
    lab_answers.grades = json.dumps(data.get('grades', {}))

    db.session.commit()
    
    return {"status": "ok", "result": _("Answers saved successfully")}, 200

@blueprint.route('/users/<int:user_id>', methods=["DELETE"])
@login_required
def delete_user(user_id):
    if current_user.category != "admin":
        return {"status": "fail", "result": _("Unauthorized access")}, 401

    user = db.session.get(Users, user_id)
    if not user or user.is_deleted:
        return {"status": "fail", "result": _("User not found")}, 404

    labs = LabInstances.query.filter_by(user_id=user_id, is_deleted=False)
    if labs.count() > 0:
        return {"status": "fail", "result": _("Failed to delete user: user has labs running")}, 400

    _soft_delete_user(user)

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to delete user {user_id}: {exc}")
        return {"status": "fail", "result": _("Failed to delete user")}, 400

    return {"status": "ok", "result": _("User deleted successfully")}, 200


def _soft_delete_user(user):
    """Mark a user as deleted, snapshotting and clearing its group memberships.

    Does not commit - the caller is responsible for that.
    """
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


@blueprint.route('/users/bulk', methods=["DELETE"])
@login_required
def delete_users():
    if current_user.category != "admin":
        return {"status": "fail", "result": _("Unauthorized access")}, 401

    content = request.get_json(silent=True)
    if not content or not isinstance(content, list):
        return {"status": "fail", "result": _("invalid content")}, 400

    users = []
    errors = []
    for user_id in content:
        if not isinstance(user_id, str) or not user_id.isdigit():
            errors.append(f"Invalid user provided {user_id=}")
            continue
        user = db.session.get(Users, int(user_id))
        if not user or user.is_deleted:
            errors.append(f"User not found {user_id=}")
            continue
        if LabInstances.query.filter_by(user_id=user.id, is_deleted=False).count() > 0:
            errors.append(f"User has labs running {user_id=}")
            continue
        users.append(user)

    if errors:
        return {"status": "fail", "result": _("Invalid users to delete:") + " " + "<br/>".join(errors)}, 400

    for user in users:
        _soft_delete_user(user)

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to delete users: {exc}")
        return {"status": "fail", "result": _("Failed to delete users")}, 400

    return {"status": "ok", "result": _("Users deleted successfully")}, 200


@blueprint.route('/groups/<int:group_id>', methods=["DELETE"])
@login_required
def delete_group(group_id):
    if current_user.category not in ["admin", "teacher"]:
        return {"status": "fail", "result": _("Unauthorized access")}, 401

    group = db.session.get(Groups, group_id)
    if not group or group.is_deleted:
        return {"status": "fail", "result": _("Group not found")}, 404

    if group.organization == "SYSTEM" and current_user.category != "admin":
        return {"status": "fail", "result": _("Only admins can change System groups")}, 404

    if current_user.category == "teacher" and not group.is_owner(current_user.id):
        return {"status": "fail", "result": _("Unauthorized access to this group")}, 401

    group.is_deleted = True
    deleted = DeletedGroupUsers()
    deleted.object_id = group.id
    deleted.object_type = "group"
    deleted.members = json.dumps(list(group.members_dict.keys()))
    deleted.assistants = json.dumps(list(group.assistants_dict.keys()))
    deleted.owners = json.dumps(list(group.owners_dict.keys()))
    db.session.add(deleted)
    group.members.clear()
    group.assistants.clear()
    group.owners.clear()

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to delete group {group_id}: {exc}")
        return {"status": "fail", "result": _("Failed to delete group")}, 400

    return {"status": "ok", "result": _("Group deleted successfully")}, 200


@blueprint.route('/lab_categories/<int:category_id>', methods=["DELETE"])
@login_required
def delete_lab_category(category_id):
    if current_user.category != "admin":
        return {"status": "fail", "result": _("Unauthorized access")}, 401

    category = db.session.get(LabCategories, category_id)
    if not category or category.is_deleted:
        return {"status": "fail", "result": _("Lab Category not found")}, 404

    if len(category.labs) > 0:
        return {"status": "fail", "result": _("Cannot delete: Lab Category is still in use by %(n)s lab(s)", n=len(category.labs))}, 400

    category.is_deleted = True
    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to delete lab category {category_id}: {exc}")
        return {"status": "fail", "result": _("Failed to delete lab category")}, 400

    return {"status": "ok", "result": _("Lab Category deleted successfully")}, 200


@blueprint.route('/labs/<lab_id>', methods=["DELETE"])
@login_required
def delete_lab_catalog(lab_id):
    if current_user.category not in ["admin", "teacher", "labcreator"]:
        return {"status": "fail", "result": _("Unauthorized access")}, 401

    lab = db.session.get(Labs, lab_id)
    if not lab or lab.is_deleted:
        return {"status": "fail", "result": _("Lab not found")}, 404

    # non-admins (teacher/labcreator) may only delete labs they own
    if current_user.category != "admin" and lab.updated_by != current_user.id:
        return {"status": "fail", "result": _("Unauthorized access to this lab")}, 401

    active = LabInstances.query.filter_by(lab_id=lab_id, is_deleted=False).count()
    if active > 0:
        return {"status": "fail", "result": _("Cannot delete: Lab has %(n)s running instance(s)", n=active)}, 400

    lab.is_deleted = True
    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to delete lab {lab_id}: {exc}")
        return {"status": "fail", "result": _("Failed to delete lab")}, 400

    # remove the lab-data ConfigMaps from the cluster; files stay on disk so a
    # restore can recreate them. Best effort: never blocks the delete.
    try:
        k8s.delete_labdata_configmaps(lab_id)
    except Exception as exc:
        current_app.logger.error(f"Failed to delete lab-data ConfigMaps for lab {lab_id}: {exc}")

    return {"status": "ok", "result": _("Lab deleted successfully")}, 200


@blueprint.route('/labs/<lab_id>/restore', methods=["POST"])
@login_required
def restore_lab_catalog(lab_id):
    if current_user.category != "admin":
        return {"status": "fail", "result": _("Unauthorized access")}, 401

    lab = db.session.get(Labs, lab_id)
    if not lab or not lab.is_deleted:
        return {"status": "fail", "result": _("Lab not found")}, 404

    lab.is_deleted = False
    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to restore lab {lab_id}: {exc}")
        return {"status": "fail", "result": _("Failed to restore lab")}, 400

    # recreate the lab-data ConfigMaps that were removed when the lab was deleted
    labdata = lab.lab_metadata.md.get("labdata", []) if lab.lab_metadata else []
    if labdata:
        from apps.home.routes import _labdata_dir
        try:
            k8s.sync_labdata_configmaps(lab_id, labdata, _labdata_dir(lab_id))
        except Exception as exc:
            current_app.logger.error(f"Failed to restore lab-data ConfigMaps for lab {lab_id}: {exc}")

    return {"status": "ok", "result": _("Lab restored successfully")}, 200


@blueprint.route('/groups/join/<int:group_id>', methods=["POST"])
@login_required
def join_group(group_id):
    content = request.get_json(silent=True)
    if not content or not content.get("accessToken"):
        return {"status": "fail", "result": _("invalid content")}, 400

    group = db.session.get(Groups, group_id)
    if not group or group.is_deleted:
        return {"status": "fail", "result": _("Group not found")}, 404

    if group.organization == "SYSTEM" and current_user.category != "admin":
        return {"status": "fail", "result": _("Only admins can change System groups")}, 404

    if not group.accesstoken or group.accesstoken != content.get("accessToken"):
        return {"status": "fail", "result": _("Invalid group access token")}, 400

    if group.is_member(current_user.id):
        return {"status": "ok", "result": _("Already member of group")}, 200

    group.members.append(current_user)

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to join group {group_id}: {exc}")
        return {"status": "fail", "result": _("Failed to join group")}, 400

    if check_pre_approved(current_user):
        db.session.commit()

    return {"status": "ok", "result": _("Joint group successfully! Click on 'Reload profile' to update your authorization.")}, 200

@blueprint.route('/lab_answers/check/<lab_id>/<int:answer_id>')
@login_required
def check_lab_answer(lab_id, answer_id):
    if current_user.category not in ["admin", "teacher"]:
        return {"status": "fail", "result": _("Unauthorized access")}, 401

    lab = db.session.get(Labs, lab_id)
    if not lab:
        return {"status": "fail", "result": _("Invalid or Unauthorized access to lab")}, 401

    mygroups = current_user.privileged_group_ids
    for group in lab.allowed_groups:
        if group.id in mygroups:
            break
    else:
        return {"status": "fail", "result": _("Invalid or Unauthorized access to lab")}, 401

    lab_answer = db.session.get(LabAnswers, answer_id)
    if not lab_answer:
        return {"status": "fail", "result": _("Invalid or Unauthorized access to lab answer")}, 401

    lab_answer_sheet = LabAnswerSheet.query.filter_by(lab_id=lab_id).first()
    if not lab_answer_sheet:
        return {"status": "fail", "result": _("No Lab Answer Sheet available. Please create the Answer Sheet first.")}, 400

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
        return {"status": "fail", "result": _("Unauthorized access")}, 401

    user_feedbacks = cache.get("user_feedbacks")
    if user_feedbacks is None:
        user_feedbacks = UserFeedbacks.query.filter_by(is_hidden=False).order_by(UserFeedbacks.created_at.desc()).limit(5).all()
        user_feedbacks = [fb.as_dict() for fb in user_feedbacks]
        cache.set("user_feedbacks", user_feedbacks)

    if request.method == "GET":
        return {"status": "ok", "recent_feedbacks": user_feedbacks}, 200

    data = request.get_json()
    stars = data.get("stars")
    comment = data.get("comment", "")

    if not stars:
        return {"status": "fail", "result": _("Stars/Rating mandatory")}, 400

    user_feedback = UserFeedbacks.query.filter_by(user_id=current_user.id).first()
    is_new = False
    if not user_feedback:
        is_new = True
        user_feedback = UserFeedbacks(user_id=current_user.id)
        db.session.add(user_feedback)

    user_feedback.stars = stars
    user_feedback.comment = comment
    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to save user feedback for {current_user.id}: {exc}")
        return {"status": "fail", "result": _("Failed to save user feedback")}, 400

    if is_new:
        user_feedbacks.insert(0, user_feedback.as_dict())
        if len(user_feedbacks) > 5:
            user_feedbacks.pop(-1)
    else:
        for fb in user_feedbacks:
            if fb["user_id"] == current_user.id:
                fb["stars"] = stars
                fb["comment"] = comment
                break
    cache.set("user_feedbacks", user_feedbacks)

    return {
        "status": "ok",
        "result": _("Feedback given successfully"),
        "recent_feedbacks": user_feedbacks,
    }, 200


@blueprint.route('/user_like', methods=["POST"])
@login_required
def add_user_like():
    counter = cache.get("user_likes") or UserLikes.query.count()
    user_like = db.session.get(UserLikes, current_user.id)
    if user_like:
        return {"status": "ok", "result": counter}, 200
    user_like = UserLikes(user_id=current_user.id)
    db.session.add(user_like)
    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to add user like: {exc}")
        return {"status": "fail", "result": _("Failed to add user like")}, 400
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
        return {"status": "fail", "result": _("Failed to delete user like")}, 400
    counter = cache.get("user_likes") or UserLikes.query.count()
    counter = max(counter-1, 0)
    cache.set("user_likes", counter)
    return {"status": "ok", "result": counter}, 200

@blueprint.route('/lab/<lab_id>/extend', methods=["POST"])
@login_required
def extend_lab(lab_id):

    lab_instance = db.session.get(LabInstances, lab_id)
    if not lab_instance:
        return {"status": "fail", "result": _("Lab instance not found")}, 404

    if current_user.category != "admin" and lab_instance.user_id != current_user.id:
        return {"status": "fail", "result": _("Unauthorized access to this lab")}, 401

    content = request.get_json(silent=True)
    if not content or 'extend_hours' not in content:
        return {"status": "fail", "result": _("Invalid content")}, 400

    extend_hours = content['extend_hours']
    if not isinstance(extend_hours, int) or extend_hours <= 0 or extend_hours > 720:
        return {"status": "fail", "result": _("Invalid extend hours")}, 400

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
        return {"status": "fail", "result": _("Failed updating lab duration, please contact the administrator.")}, 400

    current_app.logger.error(
        "Lab duration/expiration extended successfully: "
            f"{lab_id=} {current_user.id=} {new_expiration=}"
    )

    return {"status": "ok", "result": new_expiration}, 200


@blueprint.route('/templates/list')
@login_required
@check_user_category(["admin", "teacher"])
def list_kubernetes_templates():
    git_url = current_app.config.get('LAB_TEMPLATES_GIT_URL')
    git_dir = current_app.config.get('LAB_TEMPLATES_DIR')
    refresh = current_app.config.get('LAB_TEMPLATES_REFRESH')
    force_refresh = request.args.get('force_refresh')
    if not git_url:
        return {"status": "not-defined", "result": []}, 200
    if force_refresh:
        refresh = 0
    try:
        git.update_repo(git_url, git_dir, refresh_interval=refresh)
        template_files = [
            f.removesuffix(".yaml")
            for f in git.list_files(git_dir, pattern="**/*.yaml")
        ]
        return {"status": "ok", "result": template_files}, 200
    except Exception as e:
        return {"status": "fail", "result": _("Failed to list templates")}, 400


@blueprint.route('/templates/<template_name>', methods=['GET'])
@login_required
@check_user_category(["admin", "teacher"])
def get_kubernetes_template(template_name):
    templates_dir = current_app.config.get('LAB_TEMPLATES_DIR')
    template_name = secure_filename(template_name)
    status, result = git.get_file(templates_dir, f'{template_name}.yaml')
    if not status:
        return {"status": "fail", "result": result}, 400
    return {"status": "ok", "result": result}, 200


@blueprint.route('/support/thread', methods=["GET"])
@login_required
def get_support_thread():
    """Return the current user's active support thread and its messages."""
    thread = support.get_active_thread(current_user)
    if thread is None:
        return {"thread": None}, 200
    support.mark_thread_seen_by_user(thread)
    db.session.commit()
    return {"thread": thread.as_dict(with_messages=True)}, 200


@blueprint.route('/support/thread/messages', methods=["POST"])
@login_required
def post_support_message():
    """Persist a user message and store any auto-reply.

    Support is notified by the batched ``flush-support-emails`` CLI job, not here.
    """
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return {"error": _("message body is required")}, 400

    # If the widget is posting into a specific conversation, it must still be open.
    thread_id = data.get("thread_id")
    if thread_id is not None:
        thread = db.session.get(SupportThreads, thread_id)
        if thread is None or thread.user_id != current_user.id:
            return {"status": "fail", "result": _("Thread not found")}, 404
        if thread.status == "finished":
            reason = next(
                (m.body for m in reversed(thread.messages) if m.sender == "system"),
                "This conversation has been finished.",
            )
            return {"error": reason + " Please start a new conversation.", "finished": True}, 409
        is_new = False
    else:
        is_new = support.get_active_thread(current_user) is None
        # ``mode`` applies to a thread being created (the widget's chooser);
        # an ongoing conversation keeps the mode it started with.
        thread = support.get_or_create_active_thread(
            current_user, mode=data.get("mode"), locale=_current_locale()
        )

    message = support.add_message(thread, "user", body, is_read=False)
    if is_new:
        support.record_telemetry(
            thread,
            page=(data.get("page") or "").strip() or None,
            user_agent=request.headers.get("User-Agent"),
            ip=get_remote_addr(),
        )
    db.session.commit()

    messages = [message.as_dict()]
    reply = support.generate_support_reply(thread, body)
    if reply:
        reply_msg = support.add_message(thread, "assistant", reply, is_read=True)
        db.session.commit()
        messages.append(reply_msg.as_dict())

    return {"thread_id": thread.id, "messages": messages}, 201


@blueprint.route('/support/threads/<int:thread_id>', methods=["GET"])
@login_required
def get_support_thread_by_id(thread_id):
    """Return a single thread + messages (admin, or the thread's owner)."""
    thread = db.session.get(SupportThreads, thread_id)
    if thread is None:
        return {"status": "fail", "result": _("Thread not found")}, 404
    is_owner = thread.user_id == current_user.id
    if current_user.category != "admin" and not is_owner:
        return {"status": "fail", "result": _("Unauthorized")}, 403
    if is_owner:
        support.mark_thread_seen_by_user(thread)
        db.session.commit()
    return {"thread": thread.as_dict(with_messages=True)}, 200


@blueprint.route('/support/thread/finish', methods=["POST"])
@login_required
def finish_support_thread():
    """Finish the current user's active thread, if any."""
    thread = support.get_active_thread(current_user)
    if thread is None:
        return {"status": "ok"}, 200
    support.finish_thread(thread, by="user")
    db.session.commit()
    return {"status": "ok", "thread_id": thread.id}, 200


@blueprint.route('/support/threads/<int:thread_id>/finish', methods=["POST"])
@login_required
def finish_support_thread_admin(thread_id):
    """Finish any thread (admin only)."""
    if current_user.category != "admin":
        return {"status": "fail", "result": _("Unauthorized")}, 403

    thread = db.session.get(SupportThreads, thread_id)
    if thread is None:
        return {"status": "fail", "result": _("Thread not found")}, 404

    support.finish_thread(thread, by="support")
    db.session.commit()
    return {"status": "ok", "thread_id": thread.id}, 200


@blueprint.route('/support/threads/<int:thread_id>/messages', methods=["POST"])
@login_required
def post_support_reply(thread_id):
    """Staff reply to a thread (admin only)."""
    if current_user.category != "admin":
        return {"status": "fail", "result": _("Unauthorized")}, 403

    thread = db.session.get(SupportThreads, thread_id)
    if thread is None:
        return {"status": "fail", "result": _("Thread not found")}, 404

    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return {"error": _("message body is required")}, 400

    message = support.add_message(thread, "support", body, is_read=True)
    support.mark_thread_read(thread)
    db.session.commit()
    return {"thread_id": thread.id, "messages": [message.as_dict()]}, 201


# --- RAG assistant (doc/rag-assistant-design.md) ----------------------------
def _current_locale():
    """The locale the UI is currently rendered in ("en", "pt_BR", ...)."""
    try:
        return str(get_locale() or current_app.config.get("BABEL_DEFAULT_LOCALE", "en"))
    except Exception:
        return current_app.config.get("BABEL_DEFAULT_LOCALE", "en")


@blueprint.route('/support/assistant/status', methods=["GET"])
@login_required
def get_assistant_status():
    """Whether the widget should offer the assistant at all.

    Drives the mode chooser: when the assistant is disabled or the circuit
    breaker is open, the widget hides it and behaves exactly as it did before.
    """
    return {
        "enabled": bool(current_app.config.get("RAG_ENABLED")),
        "available": support.assistant_available(),
        "feedback": bool(current_app.config.get("RAG_STORE_TRANSCRIPTS", True)),
        "locale": _current_locale(),
    }, 200


@blueprint.route('/support/thread/mode', methods=["POST"])
@login_required
def set_support_thread_mode():
    """Set who answers the active conversation (support | assistant).

    With no active thread there is nothing to set: the widget remembers the
    choice and sends it with the first message, so picking a mode never creates
    an empty support case.
    """
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    if mode not in (support.MODE_SUPPORT, support.MODE_ASSISTANT):
        return {"error": _("invalid mode")}, 400
    if mode == support.MODE_ASSISTANT and not support.assistant_available():
        return {"error": _("The assistant is unavailable right now."), "available": False}, 409

    thread = support.get_active_thread(current_user)
    if thread is None:
        return {"status": "ok", "thread": None, "mode": mode}, 200
    if thread.status == "finished":
        return {"error": _("This conversation has been finished."), "finished": True}, 409

    support.set_thread_mode(thread, mode, locale=_current_locale())
    db.session.commit()
    return {"status": "ok", "thread_id": thread.id, "mode": thread.mode}, 200


@blueprint.route('/support/assistant/answer', methods=["POST"])
@login_required
def post_assistant_answer():
    """Answer the conversation's latest user message with the RAG assistant.

    Deliberately a *second* request rather than part of the message POST: CPU
    generation takes seconds, and this way the user's message is already
    persisted and can never be lost by a generation failure. Blocks up to
    RAG_TIMEOUT_S; gunicorn's gevent worker yields while waiting.
    """
    if not current_app.config.get("RAG_ENABLED"):
        return {"error": _("The assistant is not enabled.")}, 404

    data = request.get_json(silent=True) or {}
    thread_id = data.get("thread_id")
    if thread_id is not None:
        thread = db.session.get(SupportThreads, thread_id)
        if thread is None or thread.user_id != current_user.id:
            return {"status": "fail", "result": _("Thread not found")}, 404
    else:
        thread = support.get_active_thread(current_user)
        if thread is None:
            return {"status": "fail", "result": _("Thread not found")}, 404

    if thread.status == "finished":
        return {"error": _("This conversation has been finished."), "finished": True}, 409
    if thread.mode != support.MODE_ASSISTANT:
        return {"error": _("This conversation is handled by the support team.")}, 409

    question = next((m.body for m in reversed(thread.messages) if m.sender == "user"), None)
    if not question:
        return {"error": _("message body is required")}, 400

    if support.rate_limit_exceeded(current_user):
        return {
            "error": _(
                "You have reached the limit of assistant questions. Please wait a "
                "few minutes, or open a support case."
            ),
            "rate_limited": True,
        }, 429

    message, body, meta = support.answer_with_assistant(
        thread, question, locale=thread.locale or _current_locale()
    )
    db.session.commit()

    if message is None:
        # RAG_STORE_TRANSCRIPTS=False: shown, not stored (and not votable).
        return {
            "thread_id": thread.id,
            "stored": False,
            "messages": [
                {
                    "id": None,
                    "thread_id": thread.id,
                    "sender": "assistant",
                    "body": body,
                    "sources": meta.get("sources") or [],
                    "feedback": None,
                    "created_at": None,
                }
            ],
        }, 201

    return {
        "thread_id": thread.id,
        "stored": True,
        "status": meta.get("status"),
        "messages": [message.as_dict()],
    }, 201


@blueprint.route('/support/thread/escalate', methods=["POST"])
@login_required
def escalate_support_thread():
    """Hand the assistant conversation over to human support.

    Captures telemetry at the moment of the hand-over -- the page the user gave
    up on, not the one the conversation started from.
    """
    data = request.get_json(silent=True) or {}
    thread_id = data.get("thread_id")
    if thread_id is not None:
        thread = db.session.get(SupportThreads, thread_id)
        if thread is None or thread.user_id != current_user.id:
            return {"status": "fail", "result": _("Thread not found")}, 404
    else:
        thread = support.get_active_thread(current_user)
        if thread is None:
            return {"status": "fail", "result": _("Thread not found")}, 404

    if thread.status == "finished":
        return {"error": _("This conversation has been finished."), "finished": True}, 409

    message = support.escalate_thread(
        thread,
        page=(data.get("page") or "").strip() or None,
        user_agent=request.headers.get("User-Agent"),
        ip=get_remote_addr(),
    )
    db.session.commit()
    return {
        "status": "ok",
        "thread_id": thread.id,
        "mode": thread.mode,
        "messages": [message.as_dict()] if message is not None else [],
    }, 200


@blueprint.route('/support/messages/<int:message_id>/feedback', methods=["POST"])
@login_required
def post_message_feedback(message_id):
    """Record 👍/👎 on an assistant answer (thread owner only)."""
    message = db.session.get(SupportMessages, message_id)
    if message is None:
        return {"status": "fail", "result": _("Message not found")}, 404
    thread = db.session.get(SupportThreads, message.thread_id)
    if thread is None or thread.user_id != current_user.id:
        return {"status": "fail", "result": _("Message not found")}, 404
    if message.sender != "assistant":
        return {"error": _("Only assistant answers can be rated.")}, 400

    data = request.get_json(silent=True) or {}
    vote = data.get("vote")
    if vote not in ("up", "down"):
        return {"error": _("invalid vote")}, 400

    stored = support.record_feedback(message, vote, reason=(data.get("reason") or "").strip())
    db.session.commit()
    return {"status": "ok", "message_id": message.id, "feedback": stored}, 200


@blueprint.route('/support/assistant/stats', methods=["GET"])
@login_required
def get_assistant_stats():
    """Service counters + locally computed feedback, for the admin panel."""
    if current_user.category != "admin":
        return {"status": "fail", "result": _("Unauthorized")}, 403
    return {
        "service": rag_client.stats(),
        "breaker": rag_client.get_breaker().state() if rag_client.is_configured() else {},
        "feedback": support.feedback_summary(),
    }, 200


def _get_lab_for_versions(lab_id):
    """Access rules mirror edit_lab: admin/teacher, labcreator only for own labs."""
    if current_user.category not in ("admin", "teacher", "labcreator"):
        return None, ({"status": "fail", "result": _("Unauthorized")}, 403)
    lab = db.session.get(Labs, lab_id)
    if not lab or (lab.is_deleted and current_user.category != "admin"):
        return None, ({"status": "fail", "result": _("Lab not found")}, 404)
    if current_user.category == "labcreator" and lab.updated_by != current_user.id:
        return None, ({"status": "fail", "result": _("Unauthorized")}, 403)
    return lab, None


@blueprint.route('/labs/<lab_id>/field_versions/<field>', methods=["GET"])
@login_required
def list_lab_field_versions(lab_id, field):
    """History of a tracked Lab field (manifest, lab_guide, extended_desc)."""
    lab, error = _get_lab_for_versions(lab_id)
    if error:
        return error
    if field not in lab_versions.TRACKED_FIELDS:
        return {"status": "fail", "result": _("Unknown field, tracked fields: %(fields)s", fields=", ".join(lab_versions.TRACKED_FIELDS))}, 400

    versions = []
    for v in lab_versions.list_versions(lab, field):
        author = db.session.get(Users, v.updated_by) if v.updated_by else None
        versions.append({
            "version": v.version,
            "created_at": v.created_at.strftime('%Y-%m-%d %H:%M') if v.created_at else None,
            "author": author.name if author else None,
        })
    return {"status": "ok", "result": versions}, 200


@blueprint.route('/labs/<lab_id>/field_versions/<field>/<int:version>', methods=["GET"])
@login_required
def get_lab_field_version(lab_id, field, version):
    """Content of one stored version; ?diff=1 returns a unified diff vs the current value."""
    lab, error = _get_lab_for_versions(lab_id)
    if error:
        return error
    if field not in lab_versions.TRACKED_FIELDS:
        return {"status": "fail", "result": _("Unknown field, tracked fields: %(fields)s", fields=", ".join(lab_versions.TRACKED_FIELDS))}, 400

    row = lab_versions.get_version(lab, field, version)
    if not row:
        return {"status": "fail", "result": _("Version not found")}, 404

    if request.args.get("diff"):
        return {"status": "ok", "result": lab_versions.diff_against_current(lab, row)}, 200
    return {"status": "ok", "result": row.content or ""}, 200


@blueprint.route('/labs/<lab_id>/field_versions/<field>/<int:version>', methods=["DELETE"])
@login_required
def delete_lab_field_version(lab_id, field, version):
    """Delete one stored version of a tracked Lab field."""
    lab, error = _get_lab_for_versions(lab_id)
    if error:
        return error
    if field not in lab_versions.TRACKED_FIELDS:
        return {"status": "fail", "result": _("Unknown field, tracked fields: %(fields)s", fields=", ".join(lab_versions.TRACKED_FIELDS))}, 400

    if not lab_versions.delete_version(lab, field, version):
        return {"status": "fail", "result": _("Version not found")}, 404
    db.session.commit()
    return {"status": "ok", "result": _("Version v%(v)s deleted", v=version)}, 200
