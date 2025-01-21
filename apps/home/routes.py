# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - present AppSeed.us
"""
import traceback
import uuid

from apps import db
from apps.home import blueprint
from apps.controllers import k8s
from apps.home.models import Labs, LabInstances, LabCategories
from apps.authentication.models import Users
from flask import render_template, request, current_app, redirect, url_for
from flask_login import login_required, current_user
from jinja2 import TemplateNotFound


@blueprint.route('/index')
@login_required
def index():
    if current_user.category == "user":
        return render_template('pages/waiting_approval.html')

    stats = {
        "lab_instances": 23,
        "registered_labs": 57,
        "likes": 23,
        "users": 38,
        "lab_inst_period_report": "1 Jul, 2014 - 23 Nov, 2014",
        "cpu_usage": 15,
        "cpu_capacity": 2304,
    }
    return render_template('pages/index.html', stats=stats, segment='index')


@blueprint.route('/running/')
@login_required
def running_labs():
    if current_user.category == "user":
        return render_template('pages/waiting_approval.html')

    filter_user = current_user.uid
    if current_user.category in ["admin", "teacher"]:
        filter_user = None
    try:
        running_labs = k8s.get_labs_by_user(filter_user)
    except Exception as exc:
        err = traceback.format_exc().replace("\n", ", ")
        current_app.logger.error(f"Error getting running labs: {exc} -- {err}")
        return render_template("pages/error.html", title="Error getting running labs", msg="Failed to obtain running labs. Check logs for more information.")

    registered_labs = {lab.id: lab.title for lab in Labs.query.all()}
    registered_user = {user.uid: user for user in Users.query.all()}

    labs = []
    for lab_id, user_uid in running_labs:
        user = registered_user.get(user_uid)
        if not user:
            current_app.logger.warning(f"Inconsistency found on running lab: owner user not found on database {user_uid=} ({lab_id=})")
            continue
        lab_inst = LabInstances.query.filter_by(lab_id=lab_id, user_id=user.id, active=True).first()
        if not lab_inst:
            current_app.logger.warning(f"Inconsistency found on running lab: lab_instance not found for {lab_id=} {user_uid=}")
            # TODO: create an lab instance? created from command line?
            continue
        lab_dict = {
            "title": registered_labs.get(lab_id, f"Unknow Lab {lab_id}"),
            "lab_id": lab_id,
            "lab_instance_id": lab_inst.id,
            "user": f"{user.name} ({user.email or 'NO-EMAIL'})",
            "resources": [],
        }
        created = None
        for pod in running_labs[(lab_id, user_uid)]:
            if pod["kind"] != "pod":
                continue
            #if pod["kind"] == "service":
            #    status = ",".join(pod["ports"])
            #    links = pod["links"]
            #else:
            #    status = f"{pod['containers_ready']}/{pod['containers_total']}"
            #    links = pod["containers"]
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
        labs.append(lab_dict)

    return render_template("pages/running.html", segment="running", labs=labs)

@blueprint.route('/run_lab/<lab_id>', methods=["GET", "POST"])
@login_required
def run_lab(lab_id):
    if current_user.category == "user":
        return render_template('pages/waiting_approval.html')

    msg_error = ""
    lab = Labs.query.get(lab_id)
    if not lab:
        return render_template("pages/error.html", title="Error Running Labs", msg="Lab not found")

    already_running = LabInstances.query.filter_by(lab_id=lab_id, user_id=current_user.id, active=True).first()

    if(current_user.category == "student" and (already_running.user_id != current_user.id)):
        return render_template("pages/error.html", title="Error Running Labs", msg="You are not authorized to run this lab")

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
        db.session.commit()
        return render_template("pages/run_lab_status.html", resources=k8s_resources, lab_instance_id=pod_hash)
    else:
        return render_template("pages/error.html", title="Error Running Labs", msg=msg)

@blueprint.route('/lab_status/<lab_id>', methods=["GET"])
@login_required
def check_lab_status(lab_id):
    if current_user.category == "user":
        return render_template('pages/waiting_approval.html')

    msg_error = ""
    lab = LabInstances.query.get(lab_id)
    if(current_user.category == "student" and (lab.user_id != current_user.id)):
        return render_template("pages/error.html", title="Error checking lab status", msg="You are not authorized to run this lab")

    if not lab:
        return render_template("pages/error.html", title="Error checking lab status", msg="Lab not found")

    return render_template("pages/run_lab_status.html", resources=lab.k8s_resources, lab_instance_id=lab_id)

@blueprint.route('/xterm/<lab_id>/<kind>/<pod>/<container>', methods=["GET"])
@login_required
def xterm(lab_id, kind, pod, container):
    if current_user.category == "user":
        return render_template('pages/waiting_approval.html')
    
    lab = LabInstances.query.get(lab_id)
    if not lab:
        return render_template("pages/error.html", title="Error checking lab status", msg="Lab not found")
    if(current_user.category == "student" and (lab.user_id != current_user.id)):
        return render_template("pages/error.html", title="Error checking lab status", msg="You are not authorized to run this lab")

    return render_template('pages/xterm.html', host=f"{kind}/{pod}/{container}", container=container), 200


@blueprint.route('/users/<int:user_id>', methods=["GET", "POST"])
@blueprint.route('/profile', methods=["GET", "POST"])
@login_required
def edit_user(user_id=None):
    if current_user.category == "user":
        return render_template('pages/waiting_approval.html')

    return_path = "home_blueprint.view_users"
    if not user_id:
        return_path = "home_blueprint.index"
        user_id = current_user.id

    if current_user.id == user_id and request.method == "GET":
        return render_template("pages/edit_user.html", user=current_user)

    if current_user.id != user_id and current_user.category not in ["admin", "teacher"]:
        return render_template("pages/error.html", title="Unauthorized access", msg="You dont have access for this page")

    user = Users.query.get(user_id)
    if not user or not user.active:
        return render_template("pages/error.html", title="Invalid user", msg="User not found or deactivated on the database")

    if request.method == "GET":
        return render_template("pages/edit_user.html", user=user, return_path=return_path)

    has_changed = False
    if current_user.category == "teacher" and request.form["user_category"] == "student":
        user.category = "student"
        has_changed = True

    if current_user.category == "admin":
        user.category = request.form["user_category"]
        has_changed = True

    if current_user.category == "admin" or current_user.id == user.id:
        user.username = request.form["username"]
        user.email = request.form["email"]
        user.given_name = request.form["given_name"]
        user.family_name = request.form["family_name"]
        has_changed = True
        
    if current_user.id == user.id and request.form["password"]:
        user.set_password(request.form["password"])
        has_changed = True

    if not has_changed:
        return render_template("pages/edit_user.html", msg_fail="No changes applied.", user=user, return_path=return_path)

    try:
        db.session.commit()
        status = True
        msg = "User profile updated successfully"
    except Exception as exc:
        status = False
        msg = "Failed to update user profile"
        current_app.logger.error(f"{msg} - {exc}")

    if status:
        return render_template("pages/edit_user.html", msg_ok=msg, user=user, return_path=return_path)
    else:
        return render_template("pages/edit_user.html", msg_fail=msg, user=user, return_path=return_path)


@blueprint.route('/lab_instance/view/<lab_id>', methods=["GET"])
@login_required
def view_lab_instance(lab_id):
    if current_user.category == "user":
        return render_template('pages/waiting_approval.html')

    lab_instance = LabInstances.query.get(lab_id)
    if not lab_instance:
        return render_template("pages/error.html", title="Error accessing Lab Instance", msg="Lab not found")
    if lab_instance.user_id != current_user.id and current_user.category != "admin":
        return render_template("pages/error.html", title="Error accessing Lab Instance", msg="Not authorized to access this Lab")

    lab = Labs.query.get(lab_instance.lab_id)
    if not lab:
        return render_template("pages/error.html", title="Error accessing Lab Instance", msg="Lab instance belongs to an unknown Lab.")

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
def edit_lab(lab_id):
    if current_user.category == "user":
        return render_template('pages/waiting_approval.html')

    if lab_id != "new":
        lab = Labs.query.get(lab_id)
        if not lab:
            return render_template("pages/labs_edit.html", segment="/labs/edit", msg_fail="Lab not found")
    else:
        lab = Labs()
    lab_categories = {cat.id: cat for cat in LabCategories.query.all()}
    if request.method == "GET":
        return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, segment="/labs/edit")

    # TODO: data validation/sanitization
    # validate manifest using k8s dry-run?
    # validate lab category
    # validate mandatory fields
    # ...
    lab.title = request.form["lab_title"]
    lab.description = request.form["lab_description"]
    lab.category_id = request.form["lab_category"]
    lab.set_extended_desc(request.form["lab_extended_desc"])
    lab.set_lab_guide_md(request.form["lab_guide"])
    lab.manifest = request.form["lab_manifest"]
    lab.goals = request.form.get("lab_goals", "")
    try:
        db.session.add(lab)
        db.session.commit()
        status = True
        msg = "Lab saved with success"
    except Exception as exc:
        status = False
        msg = "Failed to save Lab information"
        current_app.logger.error(f"{msg} - {exc}")
    if status:
        return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, msg_ok=msg, segment="/labs/edit")
    else:
        return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, msg_fail=msg, segment="/labs/edit")

@blueprint.route('/users')
@login_required
def view_users():
    if current_user.category == "user":
        return render_template('pages/waiting_approval.html')

    if current_user.category == "student":
        return render_template("pages/error.html", title="Unauthorized request", msg="You dont have permission to see this page")

    users = Users.query.filter(Users.active==True)
    if current_user.category in ["teacher"]:
        users = users.filter(Users.category == "user")
    users = users.all()

    return render_template("pages/users.html", users=users)


@blueprint.route('/labs/view', methods=["GET"])
@blueprint.route('/labs/view/<lab_id>', methods=["GET"])
@login_required
def view_labs(lab_id=None):
    if current_user.category == "user":
        return render_template('pages/waiting_approval.html')
    labs = Labs.query.filter()
    if lab_id:
        labs = labs.filter(Labs.id == lab_id)
    labs = labs.all()
    lab_categories = {cat.id: cat for cat in LabCategories.query.all()}
    running_labs = {lab.lab_id: lab.id for lab in LabInstances.query.filter_by(user_id=current_user.id, active=True).all()}
    return render_template("pages/labs_view.html", labs=labs, lab_categories=lab_categories, running_labs=running_labs, segment="/labs/view")


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
