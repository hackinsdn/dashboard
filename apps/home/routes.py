# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - present AppSeed.us
"""
import traceback
import uuid
import os
import re
import json
from collections import OrderedDict
from types import SimpleNamespace

from apps import db, cache
from apps.home import blueprint
from apps.controllers import k8s, c9s
from apps.controllers import support
from apps.controllers import lab_versions
from apps.home.models import Labs, LabInstances, LabCategories, LabAnswers, LabAnswerSheet, HomeLogging, UserLikes, UserFeedbacks, LabMetadata, SupportThreads, SupportMessages, generate_uuid
from apps.authentication.models import Users, Groups
from flask import render_template, request, current_app, redirect, url_for, session, send_from_directory, jsonify
from flask_babel import gettext as _
from flask_login import login_required, current_user
from jinja2 import TemplateNotFound
from apps.audit_mixin import get_remote_addr, check_user_category
from apps.authentication.forms import GroupForm
from apps.utils import update_running_labs_stats, parse_lab_expiration, parse_group_expiration, datetime_from_ts, epoch_from_datetime, update_category_stats, update_stats_lab_instances_answers, utcnow, compute_lab_score, secure_filename
from sqlalchemy import desc


# lab-data attachments live in a per-lab folder (named after the lab uuid) so
# they map 1:1 with the labdata-<uuid> ConfigMaps; the id is validated before
# it ever reaches the filesystem to avoid path traversal.
_LABDATA_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _labdata_dir(lab_id):
    return os.path.join(current_app.config['UPLOAD_DIR'], 'labdata', lab_id)


def _labdata_encoded_size(raw):
    """Size the payload will occupy inside a ConfigMap: raw bytes for text
    (stored in data), base64 length for binary (stored in binaryData)."""
    try:
        raw.decode("utf-8")
        return len(raw)
    except UnicodeDecodeError:
        return 4 * ((len(raw) + 2) // 3)


@blueprint.before_request
def get_info_before_request():
    update_running_labs_stats()

@blueprint.route('/set-locale/<locale>')
def set_locale(locale):
    """Persist the user's language choice in the session (and on their profile
    when authenticated), then return to the page they came from."""
    if locale in current_app.config["LANGUAGES"]:
        session["locale"] = locale
        if current_user.is_authenticated:
            current_user.locale = locale
            db.session.commit()
    return redirect(request.referrer or url_for("home_blueprint.index"))

@blueprint.route('/index')
@login_required
@check_user_category(["admin", "teacher", "labcreator", "student"])
def index():
    user_likes = cache.get("user_likes")
    if user_likes is None:
        user_likes = UserLikes.query.count()
        cache.set("user_likes", user_likes)

    stats_data = cache.get("stats_data")
    if stats_data is None:
        try:
            stats_data = k8s.get_statistics()
        except Exception as e:
            stats_data = {}
            current_app.logger.error(f"Failed to retrieve Kubernetes data: {e}")
        stats_data["lab_instances"] = LabInstances.query.filter_by(is_deleted=True).count()
        stats_data["users"] = Users.query.filter_by(is_deleted=False).count()
        stats_data["labs"] = Labs.query.filter_by(is_deleted=False).order_by(Labs.created_at.desc()).all()
        stats_data["lab_categories"] = update_category_stats()
        stats_data["lab_usage"] = update_stats_lab_instances_answers()
        cache.set("stats_data", stats_data)

    stats = {
        "lab_instances": stats_data.get("lab_instances", 0),
        "registered_labs": stats_data.get("labs", []),
        "lab_categories": stats_data.get("lab_categories", {}),
        "lab_usage": stats_data.get("lab_usage", {}),
        "likes": user_likes,
        "has_liked": db.session.get(UserLikes, current_user.id),
        "users": stats_data.get("users", 0),
        "lab_inst_period_report": "1 Jul, 2014 - 23 Nov, 2014",
        "cpu_capacity": stats_data.get("total_cpu_capacity", 0),
        "memory_capacity": stats_data.get("total_memory_capacity", 0),
        "storage_capacity": stats_data.get("total_storage_capacity", 0),
        "total_pods": stats_data.get("total_pods", 0),
        "total_nodes": stats_data.get("total_nodes", 0),
    }

    testbed_infos = {
        "title": current_app.config["TESTBED_TITLE"],
    }

    map_config = {
        'center': {
            'lat': current_app.config['MAP_CENTER_LAT'],
            'lng': current_app.config['MAP_CENTER_LNG']
        },
        'zoom': current_app.config['MAP_ZOOM_LEVEL'],
        'points': current_app.config['MAP_POINTS'],
    }

    user_feedback = UserFeedbacks.query.filter_by(user_id=current_user.id).first()
    show_feedback_modal = False

    if not user_feedback:
        last_shown = cache.get(f"feedback_prompt_last_shown_{current_user.id}")
        if not last_shown:
            last_shown = int(current_user.created_at.timestamp()) if current_user.created_at else 0
        now_ts = int(utcnow().timestamp())
        if now_ts - last_shown > current_app.config["HIDE_FEEDBACK_SEC"]:
            show_feedback_modal = True
            cache.set(f"feedback_prompt_last_shown_{current_user.id}", now_ts)

    return render_template('pages/index.html', stats=stats, user_feedback=user_feedback, show_feedback_modal=show_feedback_modal, testbed_infos=testbed_infos, map_config=map_config)



@blueprint.route('/running/')
@login_required
@check_user_category(["admin", "teacher", "labcreator", "student"])
def running_labs():
    filter_group = request.args.get("filter_group", "")
    filter_members = {}
    if filter_group.isdigit():
        filter_group = int(filter_group)
        group = db.session.get(Groups, filter_group)
        if not group or group.is_deleted:
            return render_template("pages/error.html", title=_("Error getting running labs"), msg=_("Group not found"))
        filter_members = group.members_dict

    registered_labs = {}
    allowed_groups_by_lab = {}
    for lab in Labs.query.all():
        registered_labs[lab.id] = lab.title
        allowed_groups_by_lab[lab.id] = {group.id: group for group in lab.allowed_groups}

    registered_user = {current_user.id: current_user}
    if filter_group:
        registered_user = {user.id: user for user in Users.query.filter_by(is_deleted=False).all()}

    current_user_groups = {}
    current_user_priv_groups = {}
    if current_user.category == "admin":
        for group in Groups.query.filter(
            Groups.is_deleted==False, Groups.organization.isnot("SYSTEM")
        ).all():
            current_user_groups[group.id] = group
    else:
        for group in current_user.member_of_groups:
            if group.organization == "SYSTEM":
                continue
            current_user_groups[group.id] = group
    for group in current_user.assistant_of_groups:
        current_user_groups[group.id] = group
        current_user_priv_groups[group.id] = group
    for group in current_user.owner_of_groups:
        current_user_groups[group.id] = group
        current_user_priv_groups[group.id] = group

    lab_instances = LabInstances.query.filter_by(is_deleted=False)
    if not filter_group:
        lab_instances = lab_instances.filter_by(user_id=current_user.id)

    labs = []
    for li in lab_instances.all():
        is_allowed = False
        # check if user has permission to see the lab
        if current_user.category == "admin" or li.user_id == current_user.id:
            is_allowed = True
        else:
            for group_id in current_user_priv_groups:
                if group_id in allowed_groups_by_lab[li.lab_id]:
                    is_allowed = True
                    break
        if not is_allowed:
            continue
        if filter_group != "all" and filter_group and li.user_id not in filter_members:
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
@check_user_category(["admin", "teacher", "labcreator", "student"])
def run_lab(lab_id):

    msg_error = ""
    lab = db.session.get(Labs, lab_id)
    if not lab:
        return render_template("pages/error.html", title=_("Error Running Labs"), msg=_("Lab not found"))

    already_running = LabInstances.query.filter_by(lab_id=lab_id, user_id=current_user.id, is_deleted=False).first()

    if already_running:
        return redirect(url_for('home_blueprint.view_lab_instance', lab_id=already_running.id))

    # XXX: we could have different expirations per user category here
    lab_expirations = OrderedDict([
        ("4", "4 hours"),
        ("24", "1 day"),
        ("168", "1 week"),
        ("720", "1 month"),
    ])
    if current_user.category == "admin":
        lab_expirations["0"] = "Never expires"

    if request.method == "GET":
        return render_template("pages/run_lab.html", lab=lab, lab_expirations=lab_expirations)

    lab_expiration = request.form.get("lab_expiration")
    if not lab_expiration or lab_expiration not in lab_expirations:
        return render_template("pages/run_lab.html", lab=lab, msg_fail=_("Invalid lab duration/expiration, please choose one of the values provided."))

    expiration_ts = parse_lab_expiration(lab_expiration)

    lab_inst = LabInstances()
    pod_hash = lab_inst.get_id()
    replace_identifiers = True
    lab_manifest = lab.manifest
    if lab.is_clab:
        replace_identifiers = False
        lab_manifest = lab_manifest.replace(f"clab-{lab.lab_metadata.short_uuid}", f"clab-{pod_hash}")
        clab_md = lab.lab_metadata.md
        topology = clab_md.get("topology")
        if not topology:
            # backwards compatibility: try with 'clab' which requires some clean up
            topology = clab_md.get("clab")
            if topology:
                topology = c9s.clean_up_for_clab_graph(topology)
        if topology:
            lab_manifest += "\n---\n" + c9s.get_topology_visualizer_manifest(pod_hash, topology)

    status, msg = k8s.create_lab(lab_id, lab_manifest, user_uid=current_user.uid, pod_hash=pod_hash, replace_identifiers=replace_identifiers)

    if status:
        lab_inst.user_id = current_user.id
        lab_inst.lab_id = lab.id
        lab_inst.k8s_resources = msg
        lab_inst.expiration_ts = expiration_ts
        db.session.add(lab_inst)

        current_app.logger.info(f"LabInstance added user={current_user.username} ipaddr={get_remote_addr()} lab_id={lab.id} lab_instance_id={pod_hash}")

        create_lab_log = HomeLogging(ipaddr=get_remote_addr(), action="create_lab", success=True, lab_id=lab.id, user_id=current_user.id)
        db.session.add(create_lab_log)
        db.session.commit()

        running_labs = LabInstances.query.filter_by(is_deleted=False, user_id=current_user.id).count()
        cache.set(f"running_labs-{current_user.id}", running_labs)

        return render_template("pages/run_lab_status.html", resources=msg, lab_instance_id=pod_hash, lab_requested_ts=epoch_from_datetime(lab_inst.created_at))
    else:
        create_lab_log_error = HomeLogging(ipaddr=get_remote_addr(), action="create_lab", success=False, lab_id=lab.id, user_id=current_user.id)
        db.session.add(create_lab_log_error)
        db.session.commit()
        return render_template("pages/error.html", title=_("Error Running Labs"), msg=msg)

@blueprint.route('/lab_status/<lab_id>', methods=["GET"])
@login_required
@check_user_category(["admin", "teacher", "labcreator", "student"])
def check_lab_status(lab_id):

    msg_error = ""
    lab = db.session.get(LabInstances, lab_id)
    if not lab:
        return render_template("pages/error.html", title=_("Error checking lab status"), msg=_("Lab not found"))

    if lab.user_id != current_user.id:
        return render_template("pages/error.html", title=_("Error checking lab status"), msg=_("You are not authorized to run this lab"))

    return render_template("pages/run_lab_status.html", resources=lab.k8s_resources, lab_instance_id=lab_id, lab_requested_ts=epoch_from_datetime(lab.created_at))

@blueprint.route('/xterm/<lab_id>/<kind>/<pod>/<container>', methods=["GET"])
@login_required
@check_user_category(["admin", "teacher", "labcreator", "student"])
def xterm(lab_id, kind, pod, container):
    lab = db.session.get(LabInstances, lab_id)
    if not lab:
        return render_template("pages/error.html", title=_("Error checking lab status"), msg=_("Lab not found"))
    if (current_user.category in ["student", "labcreator"] and (lab.user_id != current_user.id)):
        return render_template("pages/error.html", title=_("Error checking lab status"), msg=_("You are not authorized to run this lab"))

    return render_template('pages/xterm.html', host=f"{kind}/{pod}/{container}", container=container), 200


@blueprint.route('/users/<int:user_id>', methods=["GET", "POST"])
@blueprint.route('/profile', methods=["GET", "POST"])
@login_required
@check_user_category(["admin", "teacher", "labcreator", "student"])
def edit_user(user_id=None):

    return_path = "home_blueprint.view_users"
    if not user_id:
        return_path = "home_blueprint.index"
        user_id = current_user.id

    if current_user.id == user_id and request.method == "GET":
        return render_template("pages/edit_user.html", user=current_user)

    if current_user.id != user_id and current_user.category not in ["admin"]:
        return render_template("pages/error.html", title=_("Unauthorized access"), msg=_("You dont have access for this page"))

    user = db.session.get(Users, user_id)
    if not user or user.is_deleted:
        return render_template("pages/error.html", title=_("Invalid user"), msg=_("User not found or deactivated on the database"))

    if request.method == "GET":
        return render_template("pages/edit_user.html", user=user, return_path=return_path)

    if not re.match(r"^[a-zA-Z0-9_.-]{3,30}$", request.form["username"]):
        return render_template("pages/edit_user.html", msg_fail=_("Invalid username. Max size: 30. Allowed characters: a-z, A-Z, 0-9, _, . or -"), user=user, return_path=return_path)

    has_changed = False
    if current_user.category == "admin":
        user.category = request.form["user_category"]
        user.notes = request.form.get("notes", user.notes)
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
        return render_template("pages/edit_user.html", msg_fail=_("No changes applied."), user=user, return_path=return_path)

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
@check_user_category(["admin", "teacher", "labcreator", "student"])
def view_lab_instance(lab_id):

    lab_instance = db.session.get(LabInstances, lab_id)
    if not lab_instance:
        return render_template("pages/error.html", title=_("Error accessing Lab Instance"), msg=_("Lab not found"))
    if lab_instance.is_deleted:
        return render_template("pages/error.html", title=_("Error accessing Lab Instance"), msg=_("Lab finished"))

    lab = db.session.get(Labs, lab_instance.lab_id)
    if not lab:
        return render_template("pages/error.html", title=_("Error accessing Lab Instance"), msg=_("Lab instance belongs to an unknown Lab."))

    if lab_instance.user_id != current_user.id and current_user.category != "admin":
        privileged_group_ids = current_user.privileged_group_ids
        for group in lab.allowed_groups:
            if group.id in privileged_group_ids:
                break
        else:
            return render_template("pages/error.html", title=_("Error accessing Lab Instance"), msg=_("Not authorized to access this Lab"))

    owner = current_user
    if lab_instance.user_id != current_user.id:
        owner = db.session.get(Users, lab_instance.user_id)

    ports = {}
    if lab.lab_metadata:
        ports = lab.lab_metadata.md.get("ports", {})

    #running_labs = k8s.get_labs_by_user(owner.uid, lab_instance.lab_id)
    try:
        lab_resources = k8s.get_lab_resources(lab_instance.k8s_resources, published_ports=ports)
    except Exception as exc:
        err = traceback.format_exc().replace("\n", ", ")
        current_app.logger.error(f"Failed to get resources for lab_id={lab.id} lab_instance_id={lab_instance.id} exception={exc} err={err}")
        return render_template(
            "pages/error.html",
            title=_("Failed to get Lab Resources"),
            msg=_("No resource found for Lab Instance. Try again later and if the error persists, please contact the administrator."),
            additional_actions=[
                {"href": url_for("home_blueprint.view_lab_instance", lab_id=lab_instance.id), "btn-class": "btn-secondary", "icon": "fa-redo", "text": "Try again"},
                {"href": url_for("home_blueprint.cancel_restart_lab_instance", lab_id=lab_instance.id), "btn-class": "btn-warning", "icon": "fa-redo", "text": "Cancel and Restart Lab"},
            ],
        )

    #if not running_labs or (lab_instance.lab_id, owner.uid) not in running_labs:
    #if not lab_resources:
    #    return render_template("pages/error.html", title=_("Lab instance is not running"), msg=_("No resource found for Lab Instance"))

    lab_dict = {
        "title": lab.title,
        "lab_id": lab.id,
        "is_clab": lab.is_clab,
        "lab_instance_id": lab_instance.id,
        "user": f"{owner.name} ({owner.email or 'NO-EMAIL'})",
        "user_id": owner.id,
        "resources": [],
        "created": "--",
        "expires_at": datetime_from_ts(lab_instance.expiration_ts),
    }
    created = None
    #for pod in running_labs[(lab_instance.lab_id, owner.uid)]:
    for pod in lab_resources:
        #if pod["kind"] != "pod":
        #    continue
        if not created or created > pod['created']:
            created = pod["created"]
        lab_dict["resources"].append({
            "kind": "pod",
            "name": pod["name"],
            "display_name": pod["display_name"],
            "ready": pod["phase"],
            "links": pod["containers"],
            "services": pod["services"],
            "age": pod['age'],
            "node_name": pod.get("node_name", "--"),
            "pod_ip": pod.get("pod_ip", "--"),
            "labels": pod["labels"],
        })
    if created:
        lab_dict["created"] = created.strftime('%Y-%m-%d %H:%M:%S')

    return render_template("pages/lab_instance_view.html", lab=lab_dict, lab_guide=lab.lab_guide_html_str)

@blueprint.route('/lab_instance/cancel/<lab_id>', methods=["GET"])
@login_required
@check_user_category(["admin", "teacher", "student"])
def cancel_restart_lab_instance(lab_id):
    lab_instance = db.session.get(LabInstances, lab_id)
    if not lab_instance or lab_instance.is_deleted:
        return render_template("pages/error.html", title=_("Error accessing Lab Instance"), msg=_("Lab not found"))

    if lab_instance.user_id != current_user.id and current_user.category != "admin":
        return render_template("pages/error.html", title=_("Error accessing Lab Instance"), msg=_("Not authorized to access this Lab"))

    try:
        results = k8s.delete_resources_by_name(lab_instance.k8s_resources)
        assert sum(results) == len(lab_instance.k8s_resources), f"results={results} resources={lab_instance.k8s_resources}"
    except Exception as exc:
        current_app.logger.error(f"Failed to delete resources lab_instance_id={lab_instance.id}: {exc}")
        return render_template("pages/error.html", title=_("Error removing Lab Instance"), msg=_("Error removing Lab instance, please contact the administrator"))

    lab_instance.is_deleted = True
    db.session.commit()

    running_labs = LabInstances.query.filter_by(is_deleted=False, user_id=current_user.id).count()
    cache.set(f"running_labs-{current_user.id}", running_labs)

    return redirect(url_for("home_blueprint.run_lab", lab_id=lab_instance.lab_id))

@blueprint.route('/labs/edit/<lab_id>', methods=["GET", "POST"])
@login_required
@check_user_category(["admin", "teacher", "labcreator"])
def edit_lab(lab_id):

    if lab_id != "new":
        lab = db.session.get(Labs, lab_id)
        # admins may open a soft-deleted lab in order to restore it
        if not lab or (lab.is_deleted and current_user.category != "admin"):
            return render_template("pages/labs_edit.html", lab=None, segment="/labs/edit", msg_fail=_("Lab not found"))
        if current_user.category == "labcreator" and lab.updated_by != current_user.id:
            return render_template(
                "pages/error.html",
                title=_("Unauthorized access"),
                msg=_("You don't have permission to edit this Lab.")
            )
    else:
        lab = Labs()

    lab_categories = {cat.id: cat for cat in LabCategories.query.filter_by(is_deleted=False).all()}
    if not lab_categories:
        return render_template("pages/labs_edit.html", segment="/labs/edit", msg_fail=_("No Lab Categories found. Please create a Lab Category first."), lab=lab)

    groups = Groups.query.filter_by(is_deleted=False).all()

    lab_uploads = []
    lab_labdata = []
    if lab.lab_metadata:
        lab_uploads = lab.lab_metadata.md.get("uploads", [])
        lab_labdata = lab.lab_metadata.md.get("labdata", [])

    # brand-new labs get their uuid assigned upfront so lab-data uploads can
    # target a stable folder / ConfigMap name before the first DB save: GET
    # mints a fresh one, POST reuses the id the form was rendered with
    labdata_lab_id = lab.id
    if not labdata_lab_id:
        form_lab_id = request.form.get("labdata_lab_id", "").strip()
        labdata_lab_id = form_lab_id if _LABDATA_ID_RE.match(form_lab_id) else generate_uuid()

    if request.method == "GET":
        return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, groups=groups, allowed_groups=lab.allowed_groups, segment="/labs/edit", lab_uploads=lab_uploads, lab_labdata=lab_labdata, labdata_lab_id=labdata_lab_id)

    # TODO: data validation/sanitization
    # validate manifest using k8s dry-run?
    # validate mandatory fields
    # ...
    # detect tracked-field changes before the new values overwrite the lab
    new_field_values = {
        "manifest": request.form["lab_manifest"],
        "lab_guide": request.form["lab_guide"],
        "extended_desc": request.form["lab_extended_desc"],
    }
    changed_fields = {
        field: value
        for field, value in new_field_values.items()
        if (value or "") != lab_versions.current_value(lab, field)
    }

    db.session.add(lab)
    # pin brand-new labs to the upfront uuid so lab-data uploaded before this
    # first save (folder labdata/<id>/ and labdata-<uuid> ConfigMaps) lines up
    if not lab.id:
        lab.id = labdata_lab_id
    lab.title = request.form["lab_title"]
    lab.description = request.form["lab_description"]

    selected_category_ids = request.form.getlist('lab_categories')
    invalid_lab_category = ""
    if selected_category_ids:
        lab.categories.clear()
        for c_id in selected_category_ids:
            c_id = int(c_id) if isinstance(c_id, str) and c_id.isdigit() else c_id
            if not (category := lab_categories.get(c_id)):
                invalid_lab_category = f"Invalid Lab Category ({c_id}). "
                break
            lab.categories.append(category)

    lab.set_extended_desc(request.form["lab_extended_desc"])
    lab.set_lab_guide_md(request.form["lab_guide"])
    lab.manifest = request.form["lab_manifest"]
    lab.goals = request.form.get("lab_goals", "")
    selected_group_ids = request.form.getlist('lab_allowed_groups')
    lab.allowed_groups = Groups.query.filter(Groups.id.in_(selected_group_ids), Groups.is_deleted==False).all()

    # only admins may reposition labs in the listing
    if current_user.category == "admin":
        display_order_raw = request.form.get("lab_display_order", "").strip()
        try:
            lab.display_order = int(display_order_raw) if display_order_raw else 1000
        except ValueError:
            return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, msg_fail=_("Invalid display order: must be an integer number."), segment="/labs/edit", groups=groups, allowed_groups=lab.allowed_groups, lab_uploads=lab_uploads, lab_labdata=lab_labdata, labdata_lab_id=labdata_lab_id)

    if not lab.categories or invalid_lab_category:
        return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, msg_fail=invalid_lab_category+"Please select at least one category", segment="/labs/edit", groups=groups, allowed_groups=lab.allowed_groups, lab_uploads=lab_uploads, lab_labdata=lab_labdata, labdata_lab_id=labdata_lab_id)

    # snapshot the changed fields in the same transaction as the lab save
    for field, value in changed_fields.items():
        lab_versions.record_version(lab, field, value or "")

    try:
        db.session.commit()
        status = True
        msg = "Lab saved with success"
        action = "added" if lab_id == "new" else "updated"
        current_app.logger.info(f"Lab {action} user={current_user.username} ipaddr={get_remote_addr()} lab_id={lab.id} title={lab.title!r}")
    except Exception as exc:
        status = False
        msg = "Failed to save Lab information"
        current_app.logger.error(f"{msg} - {exc}")

    # Associate any pending uploads (from new-lab mode where lab.id was not yet available)
    if status:
        pending_filenames_raw = request.form.get("pending_upload_filenames", "[]")
        pending_orignames_raw = request.form.get("pending_upload_orignames", "[]")
        try:
            pending_filenames = json.loads(pending_filenames_raw)
            pending_orignames = json.loads(pending_orignames_raw)
        except Exception:
            pending_filenames = []
            pending_orignames = []

        if pending_filenames:
            upload_dir = current_app.config['UPLOAD_DIR']
            lab_md = lab.lab_metadata
            if not lab_md:
                lab_md = LabMetadata(lab=lab, is_clab=False)
                db.session.add(lab_md)
            md = lab_md.md
            existing_uploads = md.get("uploads", [])
            existing_filenames = {u["filename"] for u in existing_uploads}
            for fname, orig in zip(pending_filenames, pending_orignames):
                if fname in existing_filenames:
                    continue
                fpath = os.path.join(upload_dir, fname)
                if not os.path.exists(fpath):
                    current_app.logger.warning(f"Pending upload file not found on disk: {fname}")
                    continue
                file_url = url_for('home_blueprint.serve_upload', filename=fname)
                existing_uploads.append({"filename": fname, "original_name": orig, "url": file_url})
                existing_filenames.add(fname)
            md["uploads"] = existing_uploads
            lab_md.md = md
            try:
                db.session.commit()
            except Exception as exc:
                current_app.logger.error(f"Failed to save pending uploads metadata for lab {lab.id}: {exc}")

    # Associate pending lab-data (new-lab mode) and reconcile the per-file
    # ConfigMaps (labdata-<uuid>) with the files currently attached to the lab
    labdata_warning = None
    if status:
        try:
            pending_labdata = json.loads(request.form.get("pending_labdata", "[]"))
        except Exception:
            pending_labdata = []

        lab_md = lab.lab_metadata
        had_labdata_key = bool(lab_md) and "labdata" in lab_md.md
        if pending_labdata:
            if not lab_md:
                lab_md = LabMetadata(lab=lab, is_clab=False)
                db.session.add(lab_md)
            md = lab_md.md
            existing_labdata = md.get("labdata", [])
            existing_cm = {e["cm_uuid"] for e in existing_labdata}
            files_dir = _labdata_dir(lab.id)
            for entry in pending_labdata:
                if not isinstance(entry, dict) or entry.get("cm_uuid") in existing_cm:
                    continue
                if not os.path.exists(os.path.join(files_dir, entry.get("filename", ""))):
                    current_app.logger.warning(f"Pending lab-data file not found on disk: {entry.get('filename')}")
                    continue
                existing_labdata.append(entry)
                existing_cm.add(entry["cm_uuid"])
            md["labdata"] = existing_labdata
            lab_md.md = md
            try:
                db.session.commit()
            except Exception as exc:
                current_app.logger.error(f"Failed to save pending lab-data metadata for lab {lab.id}: {exc}")
            had_labdata_key = True

        current_labdata = lab_md.md.get("labdata", []) if lab_md else []
        if current_labdata or had_labdata_key:
            # best effort: files stay on disk even if the cluster is unreachable
            try:
                cm_ok, cm_msg = k8s.sync_labdata_configmaps(lab.id, current_labdata, _labdata_dir(lab.id))
            except Exception as exc:
                current_app.logger.error(f"Failed to sync lab-data ConfigMaps for lab {lab.id}: {exc}")
                cm_ok, cm_msg = False, str(exc)
            if not cm_ok:
                labdata_warning = _("Lab data files were saved on disk, but their Kubernetes ConfigMaps could not be synced (%(detail)s). They will be retried on the next save.", detail=cm_msg)

    edit_lab_log = HomeLogging(ipaddr=get_remote_addr(), action="edit_lab", success=status, lab_id=lab.id, user_id=current_user.id)
    db.session.add(edit_lab_log)
    db.session.commit()

    if status:
        if labdata_warning:
            # lab saved, but keep the author on the edit page so the ConfigMap
            # sync warning is visible (files are safe on disk)
            lab_labdata = lab.lab_metadata.md.get("labdata", []) if lab.lab_metadata else []
            return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, msg_ok=_("Lab saved."), msg_fail=labdata_warning, segment="/labs/edit", groups=groups, allowed_groups=lab.allowed_groups, lab_uploads=lab_uploads, lab_labdata=lab_labdata, labdata_lab_id=lab.id)
        return redirect(url_for('home_blueprint.view_labs', lab_id=lab.id))
    else:
        return render_template("pages/labs_edit.html", lab=lab, lab_categories=lab_categories, msg_fail=msg, segment="/labs/edit", groups=groups, allowed_groups=lab.allowed_groups, lab_uploads=lab_uploads, lab_labdata=lab_labdata, labdata_lab_id=labdata_lab_id)

@blueprint.route('/labs/fork/<lab_id>', methods=["GET"])
@login_required
@check_user_category(["admin", "teacher", "labcreator"])
def fork_lab(lab_id):
    source = db.session.get(Labs, lab_id)
    if not source or source.is_deleted:
        return render_template("pages/labs_edit.html", lab=None, segment="/labs/edit", msg_fail=_("Lab not found"))
    if current_user.category == "labcreator" and source.updated_by != current_user.id:
        # labcreators may fork any lab they can view (same predicate as
        # view_labs): shared with one of their groups or their own
        source_group_ids = {group.id for group in source.allowed_groups}
        if not source_group_ids.intersection(current_user.all_group_ids):
            return render_template("pages/labs_edit.html", lab=None, segment="/labs/edit", msg_fail=_("Lab not found"))

    lab_categories = {cat.id: cat for cat in LabCategories.query.filter_by(is_deleted=False).all()}
    if not lab_categories:
        return render_template("pages/labs_edit.html", segment="/labs/edit", msg_fail=_("No Lab Categories found. Please create a Lab Category first."), lab=None)

    groups = Groups.query.filter_by(is_deleted=False).all()

    # Labs.title is String(255): truncate the source title so the suffix fits
    new_title = f"{source.title} -- Fork"
    if len(new_title) > 255:
        new_title = source.title[:247] + " -- Fork"

    lab_guide_md = source.lab_guide_md_str if source.lab_guide_md else ""
    extended_desc = source.extended_desc_str if source.extended_desc else ""

    # guide attachments are shared with the source lab (same filenames and
    # URLs): nothing is written to disk on this GET, so an abandoned fork
    # leaves no orphan files behind. Deletion is reference-counted in
    # delete_lab_upload, so removing the attachment from one lab does not
    # break the other.
    lab_uploads = source.lab_metadata.md.get("uploads", []) if source.lab_metadata else []

    # plain prefill object (not a Labs instance) so the source lab and its
    # relationships are never mutated nor flushed to the session; with id=None
    # the template renders in "new lab" mode and the form posts to /labs/edit/new
    lab = SimpleNamespace(
        id=None,
        is_deleted=False,
        lab_metadata=None,
        title=new_title,
        description=source.description,
        goals=source.goals,
        manifest=source.manifest,
        display_order=source.display_order,
        categories=list(source.categories),
        extended_desc_str=extended_desc,
        lab_guide_md_str=lab_guide_md,
    )

    fork_lab_log = HomeLogging(ipaddr=get_remote_addr(), action="fork_lab", success=True, lab_id=source.id, user_id=current_user.id)
    db.session.add(fork_lab_log)
    db.session.commit()

    return render_template(
        "pages/labs_edit.html",
        lab=lab,
        lab_categories=lab_categories,
        groups=groups,
        allowed_groups=source.allowed_groups,
        segment="/labs/edit",
        lab_uploads=lab_uploads,
        pending_uploads=lab_uploads,
        forked_from=source.title,
    )

@blueprint.route('/users')
@login_required
@check_user_category(["admin", "teacher"])
def view_users():
    users = Users.query.filter_by(is_deleted=False)
    if current_user.category in ["teacher"]:
        users = users.filter(Users.category == "user")
    users = users.all()

    return render_template("pages/users.html", users=users)


@blueprint.route('/lab_categories/list')
@login_required
@check_user_category(["admin", "teacher"])
def list_lab_categories():
    lab_categories = LabCategories.query.filter_by(is_deleted=False).all()
    msg_ok = session.pop("msg_ok", None)
    return render_template("pages/lab_categories_list.html", segment="/lab_categories/list", lab_categories=lab_categories, msg_ok=msg_ok)


@blueprint.route('/support/threads')
@login_required
@check_user_category(["admin"])
def list_support_threads():
    # Default: only open (not finished) threads. ?show=all lists every thread.
    show_all = request.args.get("show") == "all"
    query = SupportThreads.query
    if not show_all:
        query = query.filter(SupportThreads.status == "open")
    threads = query.order_by(desc(SupportThreads.updated_at)).all()
    return render_template(
        "pages/support_threads.html",
        segment="/support/threads",
        threads=threads,
        show_all=show_all,
    )


@blueprint.route('/support/threads/<int:thread_id>')
@login_required
@check_user_category(["admin"])
def view_support_thread(thread_id):
    thread = db.session.get(SupportThreads, thread_id)
    if thread is None:
        return render_template("pages/error.html", title=_("Not found"), msg=_("Support thread not found"))
    # Opening a thread marks its user messages as read.
    if support.mark_thread_read(thread):
        db.session.commit()
    return render_template("pages/support_thread_view.html", segment="/support/threads", thread=thread)


@blueprint.route('/support/my')
@login_required
def list_my_support_threads():
    threads = (
        SupportThreads.query.filter_by(user_id=current_user.id)
        .order_by(desc(SupportThreads.updated_at))
        .all()
    )
    return render_template("pages/my_support_threads.html", segment="/support/my", threads=threads)


@blueprint.route('/support/my/<int:thread_id>')
@login_required
def view_my_support_thread(thread_id):
    thread = db.session.get(SupportThreads, thread_id)
    if thread is None or thread.user_id != current_user.id:
        return render_template("pages/error.html", title=_("Not found"), msg=_("Support thread not found"))
    # Opening the thread marks staff replies as seen by the user.
    support.mark_thread_seen_by_user(thread)
    db.session.commit()
    return render_template("pages/my_support_thread_view.html", segment="/support/my", thread=thread)


@blueprint.app_context_processor
def inject_support_dropdown():
    """Provide recent threads, the user unread count and the admin open-cases count
    for the navbar dropdown and sidebar."""
    if not getattr(current_user, "is_authenticated", False):
        return {
            "support_recent_threads": [],
            "support_unread_count": 0,
            "support_admin_open_count": 0,
        }
    is_admin = current_user.category == "admin"
    return {
        "support_recent_threads": support.recent_threads_for_user(current_user, limit=5),
        "support_unread_count": support.user_unread_thread_count(current_user),
        "support_admin_open_count": support.open_thread_count() if is_admin else 0,
    }


@blueprint.route('/lab_categories/edit/<category_id>', methods=["GET", "POST"])
@login_required
@check_user_category(["admin", "teacher"])
def edit_lab_category(category_id):
    valid_colors = current_app.config["LAB_CATEGORY_COLORS"]

    if category_id == "new":
        action_name = "Create"
        category = LabCategories()
    else:
        action_name = "Update"
        category = db.session.get(LabCategories, int(category_id))
        if not category or category.is_deleted:
            return render_template("pages/error.html", title=_("Not found"), msg=_("Lab Category not found"))
        if current_user.category == "teacher" and category.updated_by != current_user.id:
            return render_template(
                "pages/error.html",
                title=_("Unauthorized access"),
                msg=_("You don't have permission to edit this Lab Category (only its creator or an admin can).")
            )

    if request.method == "GET":
        return render_template("pages/lab_categories_edit.html", category=category, action_name=action_name, valid_colors=valid_colors)

    new_category_name = request.form.get("category", "").strip()
    new_color = request.form.get("color_cls")
    if not new_category_name:
        return render_template("pages/lab_categories_edit.html", msg_fail=_("Category name is required."), category=category, action_name=action_name, valid_colors=valid_colors)
    if new_color not in valid_colors:
        return render_template("pages/lab_categories_edit.html", msg_fail=_("Invalid color selected."), category=category, action_name=action_name, valid_colors=valid_colors)

    category.category = new_category_name
    category.color_cls = new_color
    if category_id == "new":
        db.session.add(category)

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to save lab category: {exc}")
        return render_template("pages/lab_categories_edit.html", msg_fail=_("Failed to save Lab Category."), category=category, action_name=action_name, valid_colors=valid_colors)

    if category_id == "new":
        session["msg_ok"] = "Lab Category created successfully"
        return redirect(url_for('home_blueprint.list_lab_categories'))
    return render_template("pages/lab_categories_edit.html", msg_ok=_("Lab Category updated successfully"), category=category, action_name=action_name, valid_colors=valid_colors)


@blueprint.route('/labs/view', methods=["GET"])
@blueprint.route('/labs/view/<lab_id>', methods=["GET"])
@login_required
@check_user_category(["admin", "teacher", "labcreator", "student"])
def view_labs(lab_id=None):

    lab_categories = {cat.id: cat for cat in LabCategories.query.filter_by(is_deleted=False).all()}
    if not lab_categories:
        return render_template("pages/error.html", title=_("No Lab Categories"), msg=_("No lab categories found. Please create a Lab Category first."))

    filter_group_id = request.args.get("filter_group", "")
    filter_group_id = int(filter_group_id) if filter_group_id.isdigit() else 0

    # only admins may reveal soft-deleted labs (to restore them)
    show_deleted = current_user.category == "admin" and request.args.get("show_deleted") == "1"

    if current_user.category == "admin":
        labs = Labs.query
        if not show_deleted:
            labs = labs.filter(Labs.is_deleted == False)
        groups = {g.id: g for g in Groups.query.filter_by(is_deleted=False).all()}
    else:
        labs = Labs.query.filter(
            Labs.is_deleted == False,
            db.or_(
                Labs.allowed_groups.any(Groups.id.in_(current_user.all_group_ids)),
                Labs.updated_by == current_user.id,
            ),
        )
        groups = {g.id: g for g in Groups.query.filter(Groups.id.in_(current_user.all_group_ids), Groups.is_deleted == False).all()}

    if filter_group_id:
        labs = labs.filter(Labs.allowed_groups.any(Groups.id == filter_group_id))

    if lab_id:
        labs = labs.filter(Labs.id == lab_id)

    labs = labs.order_by(Labs.display_order, db.func.lower(Labs.title)).all()
    user_labs_status = {}
    for lab in LabInstances.query.filter_by(user_id=current_user.id).all():
        user_labs_status.setdefault(lab.lab_id, {"is_running": False, "is_completed": False})
        if not lab.is_deleted:
            user_labs_status[lab.lab_id]["is_running"] = True
            user_labs_status[lab.lab_id]["running_id"] = lab.id
        if lab.is_deleted and lab.finish_reason is not None:
            user_labs_status[lab.lab_id]["is_completed"] = True
    return render_template(
        "pages/labs_view.html",
        labs=labs,
        lab_categories=lab_categories,
        user_labs_status=user_labs_status,
        groups=groups,
        filter_group=filter_group_id,
        show_deleted=show_deleted,
        segment="/labs/view",
    )


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
@check_user_category(["admin", "teacher", "labcreator", "student"])
def edit_group(group_id):

    if group_id == "new":
        action_name = "Create"
        group = Groups()
        if current_user.category not in ["admin", "teacher"]:
            return render_template(
                "pages/error.html",
                title=_("Unauthorized access"),
                msg=_("You don't have permission to edit this group.")
            )
    else:
        action_name = "Update"
        group = db.session.get(Groups, int(group_id))
        if not group or group.is_deleted:
            return render_template("pages/groups_edit.html", segment="/groups/edit", msg_fail=_("Group not found"))
        if (current_user.category == "teacher" and current_user not in group.owners) or (current_user.category == "student" and current_user not in group.assistants):
            return render_template(
                "pages/error.html",
                title=_("Unauthorized access"),
                msg=_("You don't have permission to edit this group.")
            )

    if current_user.category != "admin" and "SYSTEM" in [group.organization, request.form.get("organization")]:
        return render_template(
            "pages/error.html",
            title=_("Unauthorized access"),
            msg=_("Only admins can create/change System groups.")
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
        if field == "expiration":
            try:
                new_value = parse_group_expiration(new_value)
            except ValueError:
                current_app.logger.error(
                    f"Failed to update group due to invalid expiration: {new_value!r}"
                )
                return render_template(
                    "pages/groups_edit.html",
                    msg_fail=_("Invalid expiration date, please use the format YYYY-MM-DD."),
                    group=group,
                    action_name=action_name,
                    users=users_info,
                    return_path="home_blueprint.view_groups"
                )
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
            current_app.logger.warning(f"Failed to process group_members {user_id=}: user not found")
            continue
        if user.id not in current_members:
            current_app.logger.info(
                f"Adding member to group: group={group.groupname}"
                f" user={user} author={current_user}"
            )
            group.members.append(user)
            has_changes = True
        else:
            current_members.pop(user.id)
    for user in current_members.values():
        has_changes = True
        group.members.remove(user)
        current_app.logger.info(
            f"Removing member to group: group={group.groupname}"
            f" user={user} author={current_user}"
        )

    # assistants
    current_assistants = group.assistants_dict
    for user_id in request.form.getlist("group_assistants"):
        try:
            user = users[int(user_id)]
        except Exception as exc:
            current_app.logger.warning(f"Failed to process group_assistants {user_id=}: user not found")
            continue
        if user.id not in current_assistants:
            group.assistants.append(user)
            current_app.logger.info(
                f"Adding assistant to group: group={group.groupname}"
                f" user={user} author={current_user}"
            )
            has_changes = True
        else:
            current_assistants.pop(user.id)
    for user in current_assistants.values():
        has_changes = True
        group.assistants.remove(user)
        current_app.logger.info(
            f"Removing assistant to group: group={group.groupname}"
            f" user={user} author={current_user}"
        )

    # owners
    current_owners = group.owners_dict
    for user_id in request.form.getlist("group_owners"):
        try:
            user = users[int(user_id)]
        except Exception as exc:
            current_app.logger.warning(f"Failed to process group_owners {user_id=}: user not found")
            continue
        if user.id not in current_owners:
            group.owners.append(user)
            current_app.logger.info(
                f"Adding owner to group: group={group.groupname}"
                f" user={user} author={current_user}"
            )
            has_changes = True
        else:
            current_owners.pop(user.id)
    for user in current_owners.values():
        has_changes = True
        group.owners.remove(user)
        current_app.logger.info(
            f"Removing owner to group: group={group.groupname}"
            f" user={user} author={current_user}"
        )

    if not has_changes:
        return render_template(
            "pages/groups_edit.html",
            msg_fail=_("No changes were made to the group."),
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
            msg_fail=_("Failed to update group."),
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
        msg_ok=_("Group updated successfully"),
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
        return render_template("pages/lab_answers_list.html", segment="/lab_answers/list", lab_answers=[], labs=labs, groups=groups, filter_lab=filter_lab_id, filter_group=filter_group_id, msg_fail=_("Invalid Lab provided for filtering."))

    filtered_members = {}
    if filter_group_id:
        if filter_group_id not in groups:
            return render_template("pages/lab_answers_list.html", segment="/lab_answers/list", lab_answers=[], labs=labs, groups=groups, filter_lab=filter_lab_id, filter_group=filter_group_id, msg_fail=_("Invalid Group provided for filtering."))
        filtered_group = groups[filter_group_id]
        filtered_members = filtered_group.members_dict

    answer_sheet = {}
    if check_answer_sheet:
        if not filter_lab_id:
            return render_template("pages/lab_answers_list.html", lab_answers=[], labs=labs, groups=groups, filter_lab=filter_lab_id, filter_group=filter_group_id, msg_fail=_("To check with the Answer Sheet you must provide a Lab (Filter by Lab)."))
        lab_answer_sheet = LabAnswerSheet.query.filter_by(lab_id=filter_lab_id).first()
        if lab_answer_sheet:
            answer_sheet = lab_answer_sheet.answers_dict
        else:
            return render_template("pages/lab_answers_list.html", lab_answers=[], labs=labs, groups=groups, filter_lab=filter_lab_id, filter_group=filter_group_id, msg_fail=_("No Lab Answer Sheet available. Please create the Answer Sheet first."))

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
        grades = lab_answer.grades_dict
        score_value, _correct, _total = compute_lab_score(answers, grades, answer_sheet)
        score = "%.2f" % score_value if score_value is not None else "--"
        lab_answers.append({
            "id": lab_answer.id,
            "lab_title": lab.title,
            "lab_id": lab.id,
            "user": f"{user.name} ({user.email or 'NO-EMAIL'})",
            "answers": lab_answer.answers_table,
            "answers_text": lab_answer.answers,
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
        return render_template("pages/lab_answers_sheet.html", labs=labs, lab_id=lab_id, msg_fail=_("Invalid Lab provided. Please choose the Lab."))

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
            msg_fail=_("Failed to update lab answers sheet."),
            labs=labs,
            lab_id=lab_id,
            answers=answers,
        )

    return render_template("pages/lab_answers_sheet.html", labs=labs, lab_id=lab_id, answers=answers, msg_ok=_("Lab answer sheet saved!"))

@blueprint.route('/feedback/hide', methods=["POST"])
@login_required
@check_user_category(["admin"])
def hide_feedback():
    feedback_id = request.form.get("feedback_id")
    action = request.form.get("action")
    if not feedback_id or action not in ["hide", "unhide"]:
        return redirect(url_for('home_blueprint.feedback_view'))

    feedback = db.session.get(UserFeedbacks, feedback_id)
    if not feedback:
        return redirect(url_for('home_blueprint.feedback_view'))

    if action == "hide":
        feedback.is_hidden = True
    else:
        feedback.is_hidden = False
    db.session.commit()
    cache.delete("user_feedbacks")
    return redirect(request.referrer or url_for('home_blueprint.feedback_view'))


@blueprint.route('/finished_labs', methods=["GET"])
@login_required
@check_user_category(["admin", "teacher", "labcreator", "student"])
def view_finished_labs():
    filter_group = request.args.get("filter_group", "")
    filter_members = {}
    if filter_group.isdigit():
        filter_group = int(filter_group)
        group = db.session.get(Groups, filter_group)
        if not group or group.is_deleted:
            return render_template("pages/error.html", title=_("Error getting finished labs"), msg=_("Group not found"))
        filter_members = group.members_dict

    registered_labs = {}
    allowed_groups_by_lab = {}
    for lab in Labs.query.all():
        registered_labs[lab.id] = lab.title
        allowed_groups_by_lab[lab.id] = {group.id: group for group in lab.allowed_groups}

    registered_user = {current_user.id: current_user}
    if filter_group:
        registered_user = {user.id: user for user in Users.query.filter_by(is_deleted=False).all()}

    current_user_groups = {}
    current_user_priv_groups = {}
    if current_user.category == "admin":
        for group in Groups.query.filter(
            Groups.is_deleted==False, Groups.organization.isnot("SYSTEM")
        ).all():
            current_user_groups[group.id] = group
    else:
        for group in current_user.member_of_groups:
            if group.organization == "SYSTEM":
                continue
            current_user_groups[group.id] = group
    for group in current_user.assistant_of_groups:
        current_user_groups[group.id] = group
        current_user_priv_groups[group.id] = group
    for group in current_user.owner_of_groups:
        current_user_groups[group.id] = group
        current_user_priv_groups[group.id] = group

    lab_instances = LabInstances.query.filter_by(is_deleted=True)
    if not filter_group:
        lab_instances = lab_instances.filter_by(user_id=current_user.id)

    labs = []
    for li in lab_instances.order_by(desc(LabInstances.created_at)).all():
        is_allowed = False
        # check if user has permission to see the lab
        if current_user.category == "admin" or li.user_id == current_user.id:
            is_allowed = True
        else:
            for group_id in current_user_priv_groups:
                if group_id in allowed_groups_by_lab[li.lab_id]:
                    is_allowed = True
                    break
        if not is_allowed:
            continue
        if filter_group != "all" and filter_group and li.user_id not in filter_members:
            continue
        user = registered_user.get(li.user_id)
        if not user:
            current_app.logger.warning(
                "Inconsistency found on finished lab: owner user not found on database"
                f" {li.user_id=} instance={li.id} lab={li.lab_id}"
            )
            continue
        labs.append({
            "title": registered_labs.get(li.lab_id, f"Unknow Lab {li.lab_id}"),
            "lab_id": li.lab_id,
            "lab_instance_id": li.id,
            "user": f"{user.name} ({user.email or 'NO-EMAIL'})",
            "created": li.created_at.strftime('%Y-%m-%d %H:%M:%S') if li.created_at else "--",
            "finished": li.updated_at.strftime('%Y-%m-%d %H:%M:%S') if li.updated_at else "--",
            "finish_reason": li.finish_reason or "--",
        })
    return render_template("pages/finished_labs.html", segment="/finished_labs", labs=labs, groups=current_user_groups, filter_group=filter_group)

@blueprint.route('/feedback_view', methods=["GET"])
@login_required
def feedback_view():
    if current_user.category == "admin":
        feedbacks = UserFeedbacks.query.order_by(UserFeedbacks.created_at.desc()).all()
    else:
        feedbacks = UserFeedbacks.query.filter_by(is_hidden=False).order_by(UserFeedbacks.created_at.desc()).all()

    return render_template('pages/feedback_view.html', feedbacks=feedbacks)

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

@blueprint.route("/finished-lab-infos/<lab_id>", methods=["GET"])
@login_required
def view_finished_lab_infos(lab_id):
    # LTI grade passback: best-effort, the congratulations page must render
    # no matter what happens on the platform side (lazy import: the lti
    # module is optional and this blueprint is core)
    lti_grade_status = None
    lab = db.session.get(Labs, lab_id)
    if lab and current_app.config.get("ENABLE_LTI"):
        try:
            from apps.lti.grades import send_lab_result_to_lms
            lti_grade_status = send_lab_result_to_lms(current_user, lab)
        except Exception as exc:
            current_app.logger.warning(
                f"LTI grade passback failed user={current_user.username} lab={lab_id}: {exc}"
            )
    return render_template(
        "pages/finished_lab_infos.html", lab_id=lab_id,
        lti_grade_status=lti_grade_status,
    )


@blueprint.route('/uploads/<path:filename>')
@login_required
def serve_upload(filename):
    return send_from_directory(current_app.config['UPLOAD_DIR'], filename)


@blueprint.route('/labs/upload-file', methods=['POST'])
@login_required
@check_user_category(["admin", "teacher", "labcreator"])
def upload_lab_file():
    if 'file' not in request.files:
        return jsonify({"status": "fail", "result": _("No file part in the request")}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "fail", "result": _("No file selected")}), 400

    # Validate extension
    allowed_exts = current_app.config['LAB_UPLOAD_ALLOWED_EXTENSIONS']
    _basename, ext = os.path.splitext(file.filename)
    if not ext or ext[1:].lower() not in allowed_exts:
        # Also check for double extensions like .tar.gz if they exist in allowed_exts
        is_allowed = False
        lower_filename = file.filename.lower()
        for allowed_ext in allowed_exts:
            if lower_filename.endswith('.' + allowed_ext.lower()):
                is_allowed = True
                ext = '.' + allowed_ext
                break
        if not is_allowed:
            return jsonify({"status": "fail", "result": _("File extension not allowed. Allowed: %(exts)s", exts=", ".join(allowed_exts))}), 400

    # Validate size
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0) # reset pointer

    max_size = current_app.config['LAB_UPLOAD_MAX_SIZE']
    if size > max_size:
        return jsonify({"status": "fail", "result": _("File exceeds maximum allowed size (%(size)sMB)", size=max_size // (1024*1024))}), 400

    # Save directory
    upload_dir = current_app.config['UPLOAD_DIR']
    os.makedirs(upload_dir, exist_ok=True)

    # Generate unique filename using uuid
    new_filename = f"{uuid.uuid4().hex}{ext.lower()}"

    saved_path = os.path.join(upload_dir, new_filename)
    try:
        file.save(saved_path)
        file_url = url_for('home_blueprint.serve_upload', filename=new_filename)
    except Exception as exc:
        current_app.logger.error(f"Failed to save uploaded file: {exc}")
        return jsonify({"status": "fail", "result": _("Failed to save file on server")}), 500

    current_app.logger.info(f"Lab file uploaded user={current_user.username} ipaddr={get_remote_addr()} original_name={file.filename!r} saved_path={saved_path}")

    # If a lab_id was provided, persist the file reference to LabMetadata
    uploads = []
    lab_id = request.form.get("lab_id", "").strip()
    if lab_id and lab_id != "new":
        lab = db.session.get(Labs, lab_id)
        if lab:
            lab_md = lab.lab_metadata
            if not lab_md:
                lab_md = LabMetadata(lab=lab, is_clab=False)
                db.session.add(lab_md)
            md = lab_md.md
            existing_uploads = md.get("uploads", [])
            existing_uploads.append({
                "filename": new_filename,
                "original_name": file.filename,
                "url": file_url,
            })
            md["uploads"] = existing_uploads
            lab_md.md = md
            try:
                db.session.commit()
            except Exception as exc:
                current_app.logger.error(f"Failed to save upload metadata for lab {lab_id}: {exc}")
            uploads = existing_uploads

    return jsonify({
        "status": "ok",
        "url": file_url,
        "filename": file.filename,
        "saved_filename": new_filename,
        "uploads": uploads,
    }), 200


@blueprint.route("/labs/<lab_id>/uploads", methods=["GET"])
@login_required
@check_user_category(["admin", "teacher", "labcreator"])
def get_lab_uploads(lab_id):
    lab = db.session.get(Labs, lab_id)
    if not lab:
        return jsonify({"status": "fail", "result": _("Lab not found")}), 404
    if current_user.category == "labcreator" and lab.updated_by != current_user.id:
        return jsonify({"status": "fail", "result": _("Unauthorized")}), 403
    uploads = lab.lab_metadata.md.get("uploads", []) if lab.lab_metadata else []
    return jsonify({"status": "ok", "uploads": uploads}), 200


@blueprint.route("/labs/<lab_id>/uploads/<filename>", methods=["DELETE"])
@login_required
@check_user_category(["admin", "teacher", "labcreator"])
def delete_lab_upload(lab_id, filename):
    lab = db.session.get(Labs, lab_id)
    if not lab:
        return jsonify({"status": "fail", "result": _("Lab not found")}), 404
    if current_user.category == "labcreator" and lab.updated_by != current_user.id:
        return jsonify({"status": "fail", "result": _("Unauthorized")}), 403

    lab_md = lab.lab_metadata
    if not lab_md:
        return jsonify({"status": "fail", "result": _("No uploads found")}), 404

    md = lab_md.md
    uploads = md.get("uploads", [])
    original_count = len(uploads)
    uploads = [u for u in uploads if u.get("filename") != filename]
    if len(uploads) == original_count:
        return jsonify({"status": "fail", "result": _("File not found in uploads list")}), 404

    md["uploads"] = uploads
    lab_md.md = md

    # lab forking shares attachment files instead of copying them, so the
    # same filename may be referenced by other labs: only remove the file from
    # disk when this lab held the last reference
    # substring matching for LabMetadata._md.contains(filename) is correct
    # (filenames are uuid4().hex)
    still_referenced = LabMetadata.query.filter(
        LabMetadata.id != lab_md.id,
        LabMetadata._md.contains(filename),
    ).first()
    if not still_referenced:
        upload_dir = current_app.config["UPLOAD_DIR"]
        fpath = os.path.join(upload_dir, filename)
        try:
            os.remove(fpath)
        except FileNotFoundError:
            current_app.logger.warning(f"Upload file not found on disk during delete: {filename}")
        except Exception as exc:
            current_app.logger.error(f"Failed to delete upload file {filename}: {exc}")

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to update uploads metadata after delete: {exc}")
        return jsonify({"status": "fail", "result": _("Failed to update metadata")}), 500

    return jsonify({"status": "ok"}), 200


@blueprint.route('/labs/labdata/upload-file', methods=['POST'])
@login_required
@check_user_category(["admin", "teacher", "labcreator"])
def upload_labdata_file():
    """Upload one lab-data file: saved under labdata/<lab_id>/ and exposed as a
    dedicated ConfigMap (labdata-<uuid>) when the lab is saved."""
    lab_id = request.form.get("lab_id", "").strip()
    if not _LABDATA_ID_RE.match(lab_id):
        return jsonify({"status": "fail", "result": _("Invalid or missing lab id")}), 400

    if 'file' not in request.files:
        return jsonify({"status": "fail", "result": _("No file part in the request")}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "fail", "result": _("No file selected")}), 400

    # Validate extension (mirrors the Lab Guide upload, incl. double extensions)
    allowed_exts = current_app.config['LABDATA_UPLOAD_ALLOWED_EXTENSIONS']
    _basename, ext = os.path.splitext(file.filename)
    is_allowed = bool(ext) and ext[1:].lower() in allowed_exts
    if not is_allowed:
        lower_filename = file.filename.lower()
        for allowed_ext in allowed_exts:
            if lower_filename.endswith('.' + allowed_ext.lower()):
                is_allowed = True
                ext = '.' + allowed_ext
                break
    if not is_allowed:
        return jsonify({"status": "fail", "result": _("File extension not allowed. Allowed: %(exts)s", exts=", ".join(sorted(allowed_exts)))}), 400

    raw = file.read()
    max_size = current_app.config['LABDATA_UPLOAD_MAX_SIZE']
    if _labdata_encoded_size(raw) > max_size:
        return jsonify({"status": "fail", "result": _("File is too large to expose as a ConfigMap (max %(size)s KiB once encoded)", size=max_size // 1024)}), 400

    files_dir = _labdata_dir(lab_id)
    os.makedirs(files_dir, exist_ok=True)

    cm_uuid = uuid.uuid4().hex
    disk_name = f"{cm_uuid}{ext.lower()}"
    original_name = secure_filename(file.filename) or disk_name
    saved_path = os.path.join(files_dir, disk_name)
    try:
        with open(saved_path, "wb") as fh:
            fh.write(raw)
    except Exception as exc:
        current_app.logger.error(f"Failed to save lab-data file: {exc}")
        return jsonify({"status": "fail", "result": _("Failed to save file on server")}), 500

    current_app.logger.info(f"Lab-data file uploaded user={current_user.username} ipaddr={get_remote_addr()} lab_id={lab_id} original_name={file.filename!r} saved_path={saved_path}")

    entry = {
        "cm_uuid": cm_uuid,
        "configmap_name": f"labdata-{cm_uuid}",
        "filename": disk_name,
        "original_name": original_name,
        "url": url_for('home_blueprint.serve_labdata', lab_id=lab_id, filename=disk_name),
    }

    # Existing lab -> persist to LabMetadata now; brand-new lab (no DB row yet)
    # -> the client submits the entry as pending and it is associated on save.
    labdata = []
    lab = db.session.get(Labs, lab_id)
    if lab:
        if current_user.category == "labcreator" and lab.updated_by != current_user.id:
            return jsonify({"status": "fail", "result": _("Unauthorized")}), 403
        lab_md = lab.lab_metadata
        if not lab_md:
            lab_md = LabMetadata(lab=lab, is_clab=False)
            db.session.add(lab_md)
        md = lab_md.md
        existing = md.get("labdata", [])
        existing.append(entry)
        md["labdata"] = existing
        lab_md.md = md
        try:
            db.session.commit()
        except Exception as exc:
            current_app.logger.error(f"Failed to save lab-data metadata for lab {lab_id}: {exc}")
        labdata = existing

    return jsonify({"status": "ok", "entry": entry, "labdata": labdata}), 200


@blueprint.route('/labs/<lab_id>/labdata/<path:filename>')
@login_required
def serve_labdata(lab_id, filename):
    if not _LABDATA_ID_RE.match(lab_id):
        return _("Not found"), 404
    return send_from_directory(_labdata_dir(lab_id), filename)


@blueprint.route("/labs/<lab_id>/labdata/<filename>", methods=["DELETE"])
@login_required
@check_user_category(["admin", "teacher", "labcreator"])
def delete_labdata_file(lab_id, filename):
    if not _LABDATA_ID_RE.match(lab_id):
        return jsonify({"status": "fail", "result": _("Invalid lab id")}), 400
    lab = db.session.get(Labs, lab_id)
    if not lab:
        return jsonify({"status": "fail", "result": _("Lab not found")}), 404
    if current_user.category == "labcreator" and lab.updated_by != current_user.id:
        return jsonify({"status": "fail", "result": _("Unauthorized")}), 403

    lab_md = lab.lab_metadata
    md = lab_md.md if lab_md else {}
    labdata = md.get("labdata", [])
    if not any(e.get("filename") == filename for e in labdata):
        return jsonify({"status": "fail", "result": _("File not found in lab data list")}), 404
    md["labdata"] = [e for e in labdata if e.get("filename") != filename]
    lab_md.md = md

    # lab-data files are per-lab (never shared across labs), safe to remove now;
    # the matching ConfigMap is pruned on the next save reconcile
    fpath = os.path.join(_labdata_dir(lab_id), filename)
    try:
        os.remove(fpath)
    except FileNotFoundError:
        current_app.logger.warning(f"Lab-data file not found on disk during delete: {filename}")
    except Exception as exc:
        current_app.logger.error(f"Failed to delete lab-data file {filename}: {exc}")

    try:
        db.session.commit()
    except Exception as exc:
        current_app.logger.error(f"Failed to update lab-data metadata after delete: {exc}")
        return jsonify({"status": "fail", "result": _("Failed to update metadata")}), 500

    return jsonify({"status": "ok"}), 200
