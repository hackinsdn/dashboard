# -*- encoding: utf-8 -*-
"""HackInSDN"""

import json
from apps import db
from apps.api import blueprint
from apps.controllers import k8s
from apps.home.models import Labs, LabInstances, LabAnswers
from apps.authentication.models import Users, GroupMembers, Groups, MemberType
from flask import request, current_app
from flask_login import login_required, current_user
from apps.authentication.util import verify_pass

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

    lab.active = False
    db.session.commit()

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
        return {}, 404

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
        if not user or not user.active or user.category != "user":
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

@blueprint.route('/join_group/<group_id>/<accesstoken>', methods=["POST"])
@login_required
def join_group(group_id, accesstoken):
    user = Users.query.get(current_user.id)
    if not user:
        return {"status": "fail", "result": "User not found"}, 404

    group = Groups.query.get(group_id)
    if not group:
        return {"status": "fail", "result": "Group not found"}, 404
    

    if not verify_pass(accesstoken, group.accesstoken):
        return {"status": "fail", "result": "Invalid access token"}, 401
    
    user_groups = GroupMembers(user_id=user.id, group_id=group.id, member_type =  MemberType.regular.value)
    db.session.add(user_groups)
    db.session.commit()

    return {"status": "ok", "result": "User joined group"}, 200
