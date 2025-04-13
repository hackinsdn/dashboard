# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - present AppSeed.us
"""
import traceback
import uuid
import re

from apps import db, cache
from apps.home import blueprint
from apps.controllers import k8s
from apps.home.models import Labs, LabInstances, LabCategories, LabAnswers, LabAnswerSheet, HomeLogging, UserLikes, UserFeedbacks
from apps.authentication.models import Users, Groups
from flask import render_template, request, current_app, redirect, url_for, session
from flask_login import login_required, current_user
from jinja2 import TemplateNotFound
from apps.audit_mixin import get_remote_addr, check_user_category
from apps.authentication.forms import GroupForm
from apps.utils import update_running_labs_stats
from sqlalchemy.sql import func


@blueprint.before_request
def get_info_before_request():
    update_running_labs_stats()

@blueprint.route('/index')
@login_required
@check_user_category(["admin", "teacher", "student"])
def index():
    review_count = db.session.query(UserFeedbacks).count()
   
    if review_count > 0:
        valid_feedbacks = UserFeedbacks.query.filter(UserFeedbacks.stars.isnot(None)).all()

        if valid_feedbacks:
            average_stars = db.session.query(func.avg(UserFeedbacks.stars)).scalar() or 0
        else:
            average_stars = 0
    else:
        average_stars = 0

    likes = UserLikes.query.count()

    stats = {
        "lab_instances": 23,
        "registered_labs": 57,
        "average_stars": round(average_stars, 2),
        "likes":likes,
        "users": 38,
        "lab_inst_period_report": "1 Jul, 2014 - 23 Nov, 2014",
        "cpu_usage": 15,
        "cpu_capacity": 2304,
    }

    return render_template('pages/index.html', stats=stats)


@blueprint.route('/running/')
@login_required
@check_user_category(["admin", "teacher", "student"])
def running_labs():

    filter_group = request.args.get("filter_group", "")
    if filter_group.isdigit():
        filter_group = int(filter_group)

    registered_labs = {}
    allowed_groups_by_lab = {}
    for lab in Labs.query.all():
        registered_labs[lab.id] = lab.title
        allowed_groups_by_lab[lab.id] = {group.id: group for group in lab.allowed_groups}

    registered_user = {current_user.id: current_user}
    if filter_group:
        registered_user = {user.id: user for user in Users.query.filter_by(is_deleted=False).all()}

    current_user_groups = {}
    for group in current_user.member_of_groups:
        current_user_groups[group.id] = group
    for group in current_user.assistant_of_groups:
        current_user_groups[group.id] = group
    for group in current_user.owner_of_groups:
        current_user_groups[group.id] = group

    lab_instances = LabInstances.query.filter_by(is_deleted=False)
    if not filter_group:
        lab_instances = lab_instances.filter_by(user_id=current_user.id)

    labs = []
    for li in lab_instances.all():
        show_labinst = False
        current_app.logger.info(f"filter lab_instances {filter_group=} {current_user_groups=} {allowed_groups_by_lab[li.lab_id]=}")
        if filter_group == "all":
            if current_user.category == "admin":
                show_labinst = True
            else:
                for group_id in current_user_groups:
                    if group_id in allowed_groups_by_lab[li.lab_id]:
                        show_labinst = True
        elif filter_group:
            if filter_group in allowed_groups_by_lab[li.lab_id]:
                show_labinst = True
        else:
            if li.user_id == current_user.id:
                show_labinst = True
        if not show_labinst:
            continue

        user = registered_user.get(li.user_id)
        if not user:
            current_app.logger.warning(
                "Inconsistency found on running lab: owner user not found on database"
                f" {li.user_id=} instance={li.id} lab={li.lab_id}"
            )
            continue

        labs.append({
            "title": registered_labs.get(li.lab_id, f"Unknow Lab {li.lab_id}"),
            "lab_id": li.lab_id,
            "lab_instance_id": li.id,
            "user": f"{user.name} ({user.email or 'NO-EMAIL'})",
            "created": li.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        })

    return render_template("pages/running.html", segment="running", labs=labs, groups=current_user_groups, filter_group=filter_group)

@blueprint.route('/run_lab/<lab_id>', methods=["GET", "POST"])
@login_required
@check_user_category(["admin", "teacher", "student"])
def run_lab(lab_id):

    msg_error = ""
    lab = Labs.query.get(lab_id)
    if not lab:
        return render_template("pages/error.html", title="Error Running Labs", msg="Lab not found")

    already_running = LabInstances.query.filter_by(lab_id=lab_id, user_id=current_user.id, is_deleted=False).first()

    if already_running:
        return redirect(url_for('home_blueprint.view_lab_instance', lab_id=already_running.id))

    if request.method == "GET":
        return render_template("pages/run_lab.html", lab=lab)

    pod_hash = uuid.uuid4().hex[:14]

    status, msg = k8s.create_lab(lab_id, lab.manifest, user_uid=current_user.uid, pod_hash=pod_hash)

    if status:
        k8s_resources = []
        for result in msg:
            for resource in result:
                k8s_resources.append({
                    "kind": resource.kind,
                    "name": resource.metadata.name,
                    "uid": resource.metadata.uid,
                })
        lab_inst = LabInstances(pod_hash, current_user, lab, k8s_resources)
        db.session.add(lab_inst)

        create_lab_log = HomeLogging(ipaddr=get_remote_addr(), action="create_lab", success=True, lab_id=lab.id, user_id=current_user.id)
        db.session.add(create_lab_log)
        db.session.commit()

        running_labs = LabInstances.query.filter_by(is_deleted=False, user_id=current_user.id).count()
        cache.set(f"running_labs-{current_user.id}", running_labs)

        return render_template("pages/run_lab_status.html", resources=k8s_resources, lab_instance_id=pod_hash)
    else:
        create_lab_log_error = HomeLogging(ipaddr=get_remote_addr(), action="create_lab", success=False, lab_id=lab.id, user_id=current_user.id)
        db.session.add(create_lab_log_error)
        db.session.commit()
        return render_template("pages/error.html", title="Error Running Labs", msg=msg)

@blueprint.route('/lab_status/<lab_id>', methods=["GET"])
@login_required
@check_user_category(["admin", "teacher", "student"])
def check_lab_status(lab_id):

    msg_error = ""
    lab = LabInstances.query.get(lab_id)
    if not lab:
        return render_template("pages/error.html", title="Error checking lab status", msg="Lab not found")
    
    if(current_user.category == "student" and (lab.user_id != current_user.id)):
        return render_template("pages/error.html", title="Error checking lab status", msg="You are not authorized to run this lab")

    return render_template("pages/run_lab_status.html", resources=lab.k8s_resources, lab_instance_id=lab_id)

@blueprint.route('/xterm/<lab_id>/<kind>/<pod>/<container>', methods=["GET"])
@login_required
@check_user_category(["admin", "teacher", "student"])
def xterm(lab_id, kind, pod, container):
    lab = LabInstances.query.get(lab_id)
    if not lab:
        return render_template("pages/error.html", title="Error checking lab status", msg="Lab not found")
    if(current_user.category == "student" and (lab.user_id != current_user.id)):
        return render_template("pages/error.html", title="Error checking lab status", msg="You are not authorized to run this lab")

    return render_template('pages/xterm.html', host=f"{kind}/{pod}/{container}", container=container), 200


@blueprint.route('/users/<int:user_id>', methods=["GET", "POST"])
@blueprint.route('/profile', methods=["GET", "POST"])
@login_required
@check_user_category(["admin", "teacher", "student"])
def edit_user(user_id=None):

    return_path = "home_blueprint.view_users"
    if not user_id:
        return_path = "home_blueprint.index"
        user_id = current_user.id

    if current_user.id == user_id and request.method == "GET":
        return render_template("pages/edit_user.html", user=current_user)

    if current_user.id != user_id and current_user.category not in ["admin"]:
        return render_template("pages/error.html", title="Unauthorized access", msg="You dont have access for this page")

    user = Users.query.get(user_id)
    if not user or user.is_deleted:
        return render_template("pages/error.html", title="Invalid user", msg="User not found or deactivated on the database")

    if request.method == "GET":
        return render_template("pages/edit_user.html", user=user, return_path=return_path)

    if not re.match(r"^[a-zA-Z0-9-]{1,30}$", request.form["username"]):
        return render_template("pages/edit_user.html", msg_fail="Invalid username. Max size: 30. Allowed characters: a-z, A-Z, 0-9 or -", user=user, return_path=return_path)

    has_changed = False
    if current_user.category == "admin":
        user.category = request.form["user_category"]
        has_changed = True

    if current_user.category == "admin" or current_user.id == user.id:
        user.username = request.form["username"]
        user.email = request.form["email"]
        user.given_name = request.form["given_name"]
        user.family_name = request.form["family_name"]
        has_changed = True
        if request.form["password"]:
            user.set_password(request.form["password"])

    if not has_changed:
        return render_template("pages/edit_user.html", msg_fail="No changes applied.", user=user, return_path=return_path)

    try:
        edit_user_log = HomeLogging(ipaddr=get_remote_addr(), action="edit_user", success=True, user_id=user.id )
        db.session.add(edit_user_log)
        db.session.commit()
        status = True
        msg = "User profile updated successfully"
    except Exception as exc:
        edit_user_log_error = HomeLogging(ipaddr=get_remote_addr(), action="edit_user", success=False, user_id=user.id )
        db.session.add(edit_user_log_error)
        db.session.commit()
        status = False
        msg = "Failed to update user profile"
        current_app.logger.error(f"{msg} - {exc}")

    if status:
        return render_template("pages/edit_user.html", msg_ok=msg, user=user, return_path=return_path)
    else:
        return render_template("pages/edit_user.html", msg_fail=msg, user=user, return_path=return_path)


@blueprint.route('/lab_instance/view/<lab_id>', methods=["GET"])
@login_required
@check_user_category(["admin", "teacher", "student"])
def view_lab_instance(lab_id):

    lab_instance = LabInstances.query.get(lab_id)
    if not lab_instance:
        return render_template("pages/error.html", title="Error accessing Lab Instance", msg="Lab not found")

    lab = Labs.query.get(lab_instance.lab_id)
    if not lab:
        return render_template("pages/error.html", title="Error accessing Lab Instance", msg="Lab instance belongs to an unknown Lab.")

    if lab_instance.user_id != current_user.id and current_user.category != "admin":
        privileged_group_ids = current_user.privileged_group_ids
        for group in lab.allowed_groups:
            if group.id in privileged_group_ids:
                break
        else:
            return render_template("pages/error.html", title="Error accessing Lab Instance", msg="Not authorized to access this Lab")

    owner = current_user
    if lab_instance.user_id != current_user.id:
        owner = Users.query.get(lab_instance.user_id)

    running_labs = k8s.get_labs_by_user(owner.uid, lab_instance.lab_id)

    if not running_labs or (lab_instance.lab_id, owner.uid) not in running_labs:
        return render_template("pages/error.html", title="Lab instance is not running", msg="No resource found for Lab Instance")

    lab_dict = {
        "title": lab.title,
        "lab_id": lab.id,
        "lab_instance_id": lab_instance.id,
        "user": f"{owner.name} ({owner.email or 'NO-EMAIL'})",
        "user_id": owner.id,
        "resources": [],
    }
    created = None
    for pod in running_labs[(lab_instance.lab_id, owner.uid)]:
        if pod["kind"] != "pod":
            continue
        if not created or created > pod['created']:
            created = pod["created"]
        lab_dict["resources"].append({
            "kind": pod["kind"],
            "name": pod["name"],
            "ready": pod["phase"],
            "links": pod["containers"],
            "services": pod["services"],
            "age": pod['age'],
            "node_name": pod.get("node_name", "--"),
            "pod_ip": pod.get("pod_ip", "--"),
        })
    if created:
        lab_dict["created"] = created.strftime('%Y-%m-%d %H:%M:%S')
    else:
        lab_dict["created"] = "--"

    return render_template("pages/lab_instance_view.html", lab=lab_dict, lab_guide=lab.lab_guide_html_str)

@blueprint.route('/labs/edit/<lab_id>', methods=["GET", "POST"])
@login_required
@check_user_category(["admin", "teacher"])
def edit_lab(lab_id):

    if lab_id != "new":
        lab = Labs.query.get(lab_id)
        if not lab:
            return render_template("pages/labs_edit.html", segment="/labs/edit", msg_fail="Lab not found")
    else:
        lab = Labs()

    lab_categories = {cat.id: cat for cat in LabCategories.query.all()}
    if not lab_categories:
        return render_template("pages/labs_edit.html", segment="/labs/edit", msg_fail="No Lab Categories found. Please create a Lab Category first.", lab=lab)

    groups = Groups.query.filter_by(is_deleted=False).all()

    if request.method == "GET":
        return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, groups=groups, allowed_groups=lab.allowed_groups, segment="/labs/edit")

    # TODO: data validation/sanitization
    # validate manifest using k8s dry-run?
    # validate mandatory fields
    # ...

    lab.title = request.form["lab_title"]
    lab.description = request.form["lab_description"]
    lab.category_id = int(request.form["lab_category"]) if request.form.get("lab_category") else None
    lab.set_extended_desc(request.form["lab_extended_desc"])
    lab.set_lab_guide_md(request.form["lab_guide"])
    lab.manifest = request.form["lab_manifest"]
    lab.goals = request.form.get("lab_goals", "")
    selected_group_ids = request.form.getlist('lab_allowed_groups')
    lab.allowed_groups = Groups.query.filter(Groups.id.in_(selected_group_ids), Groups.is_deleted==False).all()

    if lab.category_id not in lab_categories:
        return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, msg_fail="Invalid Lab Category", segment="/labs/edit", groups=groups, allowed_groups=lab.allowed_groups)
    
    try:
        db.session.add(lab)
        db.session.commit()
        status = True
        msg = "Lab saved with success"
    except Exception as exc:
        status = False
        msg = "Failed to save Lab information"
        current_app.logger.error(f"{msg} - {exc}")

    edit_lab_log = HomeLogging(ipaddr=get_remote_addr(), action="edit_lab", success= status, lab_id=lab.id, user_id=current_user.id)
    db.session.add(edit_lab_log)
    db.session.commit()

    if status:
        return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, msg_ok=msg, segment="/labs/edit", groups=groups, allowed_groups=lab.allowed_groups)
    else:
        return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, msg_fail=msg, segment="/labs/edit", groups=groups, allowed_groups=lab.allowed_groups)

@blueprint.route('/users')
@login_required
@check_user_category(["admin", "teacher"])
def view_users():
   
    users = Users.query.filter_by(is_deleted=False)
    if current_user.category in ["teacher"]:
        users = users.filter(Users.category == "user")
    users = users.all()

    return render_template("pages/users.html", users=users)


@blueprint.route('/labs/view', methods=["GET"])
@blueprint.route('/labs/view/<lab_id>', methods=["GET"])
@login_required
@check_user_category(["admin", "teacher", "student"])
def view_labs(lab_id=None):

    lab_categories = {cat.id: cat for cat in LabCategories.query.all()}
    if not lab_categories:
        return render_template("pages/error.html", title="No Lab Categories", msg="No lab categories found. Please create a Lab Category first.")

    if current_user.category == "admin":
        labs = Labs.query
    else:
        labs = Labs.query.filter(
            Labs.allowed_groups.any(Groups.id.in_(current_user.all_group_ids))
        )

    if lab_id:
        labs = labs.filter(Labs.id == lab_id)
    
    labs = labs.all()
    running_labs = {lab.lab_id: lab.id for lab in LabInstances.query.filter_by(user_id=current_user.id, is_deleted=False).all()}
    return render_template("pages/labs_view.html", labs=labs, lab_categories=lab_categories, running_labs=running_labs, segment="/labs/view")


@blueprint.route('/groups/list')
@login_required
def list_groups():
    # even unprivileged user can see the groups!
    groups = Groups.query.filter(Groups.is_deleted==False)
    if current_user.category != "admin":
        groups = groups.filter(Groups.organization.isnot("SYSTEM"))
    groups = groups.all()
    mygroups = {group.id: group for group in current_user.member_of_groups}
    msg_ok = session.pop("msg_ok", None)
    return render_template("pages/groups_list.html", segment="/groups/list", groups=groups, mygroups=mygroups, msg_ok=msg_ok)


@blueprint.route('/groups/edit/<group_id>', methods=["GET", "POST"])
@login_required
@check_user_category(["admin", "teacher", "student"])
def edit_group(group_id):

    if group_id == "new":
        action_name = "Create"
        group = Groups()
        if current_user.category not in ["admin", "teacher"]:
            return render_template(
                "pages/error.html",
                title="Unauthorized access",
                msg="You don't have permission to edit this group."
            )
    else:
        action_name = "Update"
        group = Groups.query.get(int(group_id))
        if not group or group.is_deleted:
            return render_template("pages/groups_edit.html", segment="/groups/edit", msg_fail="Group not found")
        if (current_user.category == "teacher" and current_user not in group.owners) or (current_user.category == "student" and current_user not in group.assistants):
            return render_template(
                "pages/error.html",
                title="Unauthorized access",
                msg="You don't have permission to edit this group."
            )

    if current_user.category != "admin" and "SYSTEM" in [group.organization, request.form.get("organization")]:
        return render_template(
            "pages/error.html",
            title="Unauthorized access",
            msg="Only admins can create/change System groups."
        )

    users = {}
    users_info = {}
    for user in Users.query.filter_by(is_deleted=False).all():
        users[user.id] = user
        users_info[user.id] = f"{user.name} ({user.email or 'NO-EMAIL'})"

    if request.method == "GET":
        return render_template("pages/groups_edit.html", group=group, action_name=action_name, users=users_info)

    has_changes = False
    for field in ["groupname", "description", "organization", "expiration", "accesstoken"]:
        new_value = request.form[field] if request.form[field] else None  
        if getattr(group, field) != new_value:
            setattr(group, field, new_value)
            has_changes = True

    new_value = request.form["approved_users"]
    if new_value != group.approved_users:
        errors = []
        list_email = re.split(r"[,\t\n\r; ]+", new_value.strip()) if new_value else []
        if new_value and len(list_email) == 0:
            errors.append("invalid format for approved users")
        for email in list_email:
            if not re.match(r"^[a-zA-Z0-9.+_-]+@[a-zA-Z0-9.-]+$", email):
                errors.append(f"Invalid e-mail provided: {email}")
        if errors:
            current_app.logger.error(f"Failed to update group due to errors on approved_users: {errors}")
            group.approved_users = new_value
            return render_template(
                "pages/groups_edit.html",
                msg_fail=f"Failed to update group: invalid approved users -- {errors}",
                group=group,
                action_name=action_name,
                users=users_info,
                return_path="home_blueprint.view_groups"
            )
        group.set_approved_users(list_email)
        has_changes = True

    # members
    current_members = group.members_dict
    for user_id in request.form.getlist("group_members"):
        try:
            user = users[int(user_id)]
        except Exception as exc:
            current_app.logger.warn(f"Failed to process group_members {user_id=}: user not found")
            continue
        if user.id not in current_members:
            group.members.append(user)
            has_changes = True
        else:
            current_members.pop(user.id)
    for user in current_members.values():
        has_changes = True
        group.members.remove(user)

    # assistants
    current_assistants = group.assistants_dict
    for user_id in request.form.getlist("group_assistants"):
        try:
            user = users[int(user_id)]
        except Exception as exc:
            current_app.logger.warn(f"Failed to process group_assistants {user_id=}: user not found")
            continue
        if user.id not in current_assistants:
            group.assistants.append(user)
            has_changes = True
        else:
            current_assistants.pop(user.id)
    for user in current_assistants.values():
        has_changes = True
        group.assistants.remove(user)

    # owners
    current_owners = group.owners_dict
    for user_id in request.form.getlist("group_owners"):
        try:
            user = users[int(user_id)]
        except Exception as exc:
            current_app.logger.warn(f"Failed to process group_owners {user_id=}: user not found")
            continue
        if user.id not in current_owners:
            group.owners.append(user)
            has_changes = True
        else:
            current_owners.pop(user.id)
    for user in current_owners.values():
        has_changes = True
        current_app.logger.info(f"remove user {user}")
        group.owners.remove(user)

    if not has_changes:
        return render_template(
            "pages/groups_edit.html",
            msg_fail="No changes were made to the group.",
            group=group,
            action_name=action_name,
            users=users_info,
            return_path="home_blueprint.view_groups"
        )

    if group_id == "new":
        db.session.add(group)

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to update group: {exc}")
        return render_template(
            "pages/groups_edit.html",
            msg_fail="Failed to update group.",
            group=group,
            action_name=action_name,
            users=users_info,
            return_path="home_blueprint.view_groups"
        )

    if group_id == "new":
        session["msg_ok"] = "Group updated successfully"
        return redirect(url_for('home_blueprint.list_groups'))

    return render_template(
        "pages/groups_edit.html",
        msg_ok="Group updated successfully",
        group=group,
        action_name=action_name,
        users=users_info,
        return_path="home_blueprint.view_groups"
    )


@blueprint.route('/lab_answers/list')
@login_required
@check_user_category(["admin", "teacher"])
def list_lab_answers():

    filter_lab_id = request.args.get('filter_lab')
    filter_group_id = int(request.args.get('filter_group') or 0)
    check_answer_sheet = request.args.get('check_answer_sheet')

    mygroups = current_user.privileged_group_ids
    groups = {}
    for group in Groups.query.filter(Groups.is_deleted==False, Groups.organization.isnot("SYSTEM")).all():
        if current_user.category == "admin" or group.id in mygroups:
            groups[group.id] = group
    labs = {}
    for lab in Labs.query.all():
        if current_user.category == "admin":
            labs[lab.id] = lab
            continue
        for group in lab.allowed_groups:
            if group.id in mygroups:
                labs[lab.id] = lab
                continue

    if filter_lab_id and filter_lab_id not in labs:
        return render_template("pages/lab_answers_list.html", segment="/lab_answers/list", lab_answers=[], labs=labs, groups=groups, filter_lab=filter_lab_id, filter_group=filter_group_id, msg_fail="Invalid Lab provided for filtering.")

    filtered_members = {}
    if filter_group_id:
        if filter_group_id not in groups:
            return render_template("pages/lab_answers_list.html", segment="/lab_answers/list", lab_answers=[], labs=labs, groups=groups, filter_lab=filter_lab_id, filter_group=filter_group_id, msg_fail="Invalid Group provided for filtering.")
        filtered_group = groups[filter_group_id]
        filtered_members = filtered_group.members_dict

    answer_sheet = {}
    if check_answer_sheet:
        if not filter_lab_id:
            return render_template("pages/lab_answers_list.html", lab_answers=[], labs=labs, groups=groups, filter_lab=filter_lab_id, filter_group=filter_group_id, msg_fail="To check with the Answer Sheet you must provide a Lab (Filter by Lab).")
        lab_answer_sheet = LabAnswerSheet.query.filter_by(lab_id=filter_lab_id).first()
        if lab_answer_sheet:
            answer_sheet = lab_answer_sheet.answers_dict

    users = {user.id: user for user in Users.query.filter_by(is_deleted=False).all()}
    lab_query = LabAnswers.query
    if filter_lab_id:
        lab_query = lab_query.filter_by(lab_id=filter_lab_id)
    lab_answers = []
    for lab_answer in lab_query.all():
        user = users.get(lab_answer.user_id)
        lab = labs.get(lab_answer.lab_id)
        if not user or not lab:
            continue
        if filter_group_id and user.id not in filtered_members:
            continue
        answers = lab_answer.answers_dict
        total, correct = 0, 0
        for question, expected_answer in answer_sheet.items():
            total += 1
            try:
                if re.match(fr"^{expected_answer}$", answers.get(question)):
                    correct += 1
            except:
                continue
        score = "%.2f" % (100*correct/total) if total > 0 else "--"
        lab_answers.append({
            "id": lab_answer.id,
            "lab_title": lab.title,
            "user": f"{user.name} ({user.email or 'NO-EMAIL'})",
            "answers": lab_answer.answers_table,
            "score": score,
        })
    return render_template("pages/lab_answers_list.html", segment="/lab_answers/list", lab_answers=lab_answers, labs=labs, groups=groups, filter_lab=filter_lab_id, filter_group=filter_group_id)


@blueprint.route('/lab_answers/answer_sheet/', methods=["GET", "POST"])
@login_required
@check_user_category(["admin", "teacher"])
def add_answer_sheet():

    labs = {lab.id: lab for lab in Labs.query.all()}

    lab_id = request.args.get('lab_id')
    if not lab_id:
        return render_template("pages/lab_answers_sheet.html", labs=labs)

    if lab_id not in labs:
        return render_template("pages/lab_answers_sheet.html", labs=labs, lab_id=lab_id, msg_fail="Invalid Lab provided. Please choose the Lab.")

    answers = {}
    lab_answer_sheet = LabAnswerSheet.query.filter_by(lab_id=lab_id).first()
    if lab_answer_sheet:
        answers = lab_answer_sheet.answers_dict

    if request.method == "GET":
        return render_template("pages/lab_answers_sheet.html", labs=labs, lab_id=lab_id, answers=answers)

    answers.clear()
    for q, a in zip(request.form.getlist("question"), request.form.getlist("answer")):
        if not q:
            continue
        answers[q] = a

    if not lab_answer_sheet:
        lab_answer_sheet = LabAnswerSheet()
        lab_answer_sheet.lab_id = lab_id
        db.session.add(lab_answer_sheet)
    lab_answer_sheet.set_answers(answers)

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to update lab answer sheet: {exc}")
        return render_template(
            "pages/lab_answers_sheet.html",
            msg_fail="Failed to update lab answers sheet.",
            labs=labs,
            lab_id=lab_id,
            answers=answers,
        )

    return render_template("pages/lab_answers_sheet.html", labs=labs, lab_id=lab_id, answers=answers, msg_ok="Lab answer sheet saved!")
@blueprint.route('/gallery', methods=["GET"])
@login_required
def view_gallery():
    return render_template("pages/gallery.html")


@blueprint.route('/documentation', methods=["GET"])
@login_required
def view_documentation():
    return render_template("pages/documentation.html")


@blueprint.route('/contact', methods=["GET"])
@login_required
def view_contact():
    return render_template("pages/contact.html")
