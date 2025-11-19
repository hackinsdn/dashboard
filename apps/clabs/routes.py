# -*- encoding: utf-8 -*-
import os
import shutil
import re
from collections import OrderedDict, defaultdict
from flask import render_template, redirect, url_for, request, jsonify, current_app
from flask_login import current_user, login_required
from apps.controllers import c9s, k8s
from apps import db
from apps.authentication.models import Groups
from apps.clabs import blueprint
from apps.audit_mixin import get_remote_addr, check_user_category
from apps.home.models import Labs, LabCategories, LabMetadata, HomeLogging
from apps.utils import secure_filename, list_files


# PORT_RE: regex to match ports published on ContainerLab Topology
# CLab node ports are similar to Docker ports and accept the following cases:
# ["8080:80", "80", "192.168.1.100:8080:80", "8080:80/udp", "8080:80/tcp",
#  "[::1]:8080:80", "127.0.0.1:80/tcp", "[2001:db8:100:200::1]:80",
#  "127.0.0.1:80:8080/tcp", "8080/tcp"]
PORT_RE = re.compile(r"(\d+.\d+.\d+.\d+:|\[[0-9a-fA-F:]+\]:)?(\d+:)?(?P<port>\d+)(/\w+)?")


@blueprint.route("/upsert/", methods=["GET", "POST"])
@blueprint.route("/upsert/<clab_id>", methods=["GET", "POST"])
@login_required
@check_user_category(["admin", "teacher"])
def upsert(clab_id="new"):
    """Update or Insert a ContainerLab."""

    if clab_id != "new":
        clab = Labs.query.get(clab_id)
        if not clab or clab.is_deleted:
            return render_template("pages/clabs_upsert.html", clab=None, msg_fail="ContainerLab not found")
        if not clab.is_clab:
            return redirect(url_for('home_blueprint.edit_lab', lab_id=clab_id))
    else:
        clab = Labs()

    lab_categories = {cat.id: cat for cat in LabCategories.query.all()}
    if not lab_categories:
        return render_template("pages/clabs_upsert.html", msg_fail="No Lab Categories found. Please create a Lab Category first.", clab=clab)

    groups = Groups.query.filter_by(is_deleted=False).all()

    if not (clab_md := clab.lab_metadata):
        clab_md = LabMetadata(lab=clab, is_clab=True)

    clab_uuid = clab_md.get_short_uuid()
    clab_dir = os.path.join(current_app.config['CLABS_DIR'], clab_uuid)
    current_files = list_files(clab_dir, ignore_prefix=clab_uuid)

    if request.method == "GET":
        return render_template("pages/clabs_upsert.html", clab=clab, lab_categories=lab_categories, groups=groups, current_files=current_files)

    current_app.logger.info(f"clab_id={clab.id} short_uuid={clab_uuid} clab_guide={request.form['clab_guide']}")
    clab.title = request.form.get("clab_title", "").strip()
    clab.description = request.form.get("clab_desc", "").strip()
    clab.set_extended_desc(request.form["clab_extended_desc"])
    clab.set_lab_guide_md(request.form["clab_guide"])
    clab.manifest = (request.form.get("clab_yaml") or "").strip()
    clab.goals = request.form.get("clab_goals", "")
    selected_group_ids = request.form.getlist('clab_allowed_groups')
    clab.allowed_groups = Groups.query.filter(Groups.id.in_(selected_group_ids), Groups.is_deleted==False).all()
    selected_category_ids = request.form.getlist('clab_categories')
    clab.categories = LabCategories.query.filter(LabCategories.id.in_(selected_category_ids)).all()
    clab_category = LabCategories.query.filter(LabCategories.category=="ContainerLab").first()
    if clab_category and clab_category not in clab.categories:
        clab.categories.append(clab_category)

    if not clab.categories:
        return render_template("pages/clabs_upsert.html", clab=clab, lab_categories=lab_categories, msg_fail="Please select at least one category", groups=groups)

    giturl = request.form.get("clab_giturl", "").strip()
    files = request.files.getlist("clab_files") or []
    relative_paths = request.form.getlist("relative_paths") or []

    if not clab.manifest and not giturl and len(files) == 0:
        return jsonify({"ok": False, "result": "Provide GIT URL or files"}), 400

    max_files = current_app.config["CLABS_UPLOAD_MAX_FILES"]
    if len(files) > max_files:
        return jsonify({
            "ok": False,
            "result": f"Too many files: {files_count} (max {max_files})"
        }), 400

    changed_files = False
    for file, path in zip(files, relative_paths):
        if not path:
            current_app.logger.warning(f"ignoring file without name: {file}")
            continue
        full_path = os.path.join(clab_dir, secure_filename(path))
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        file.save(full_path)
        changed_files = True

    if changed_files:
        topo_status, topo_data = c9s.parse_clab_topology(clab_dir)
        if not topo_status:
            msg = f"Failed to parse ContainerLab topology: {topo_data}"
            current_app.logger.error(msg)
            return jsonify({"ok": False, "result": msg}), 400

        try:
            nodes = topo_data["topology"]["nodes"]
        except:
            nodes = {}
        published_ports = defaultdict(dict)
        for node_name, node in nodes.items():
            for port in node.get("ports", []):
                if match := PORT_RE.match(port):
                    published_ports[node_name][match.group("port")] = port

        md = clab_md.md
        md["clab"] = topo_data
        md["ports"] = published_ports
        clab_md.md = md

        convert_status, result = c9s.convert_clab(
            clab_dir,
            clab_uuid=clab_uuid,
            destination_namespace=current_app.config["K8S_NAMESPACE"],
        )

        #shutil.rmtree(clab_dir)
        if not convert_status:
            current_app.logger.error(f"Clabverter failed for clab.uuid={clab_uuid}: {result}")
            return jsonify({"ok": False, "result": result}), 400

        clab.manifest = result

    try:
        db.session.add(clab)
        db.session.add(clab_md)
        db.session.commit()
        status = True
        msg = "ContainerLab saved with success"
        status_code = 201
    except Exception as exc:
        status = False
        msg = "Failed to save ContainerLab information"
        status_code = 400
        current_app.logger.error(f"{msg} - {exc} -- applying rollback")
        db.session.rollback()

    upsert_clab_log = HomeLogging(ipaddr=get_remote_addr(), action="upsert_clab", success=status, lab_id=clab.id, user_id=current_user.id)
    db.session.add(upsert_clab_log)
    db.session.commit()

    current_app.logger.info(f"upsert_clab clab_id={clab_id} id={clab.id} status={status} user_id={current_user.id} files={relative_paths}")

    return jsonify({"ok": status, "result": msg, "clab_id": clab.id}), status_code
