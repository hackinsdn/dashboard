# -*- encoding: utf-8 -*-
"""HackInSDN"""

import json
from apps import db, cache
from apps.api import blueprint
from apps.controllers import k8s
from apps.home.models import Labs, LabInstances, LabAnswers, UserLikes
from apps.authentication.models import Users, Groups, DeletedGroupUsers
from flask import request, current_app
from flask_login import login_required, current_user

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

    lab.is_deleted = True
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
