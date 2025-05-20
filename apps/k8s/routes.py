# -*- encoding: utf-8 -*-
"""HackInSDN"""

from apps.k8s import blueprint
from apps.controllers import k8s
from apps.authentication.models import Users, Groups, DeletedGroupUsers
from flask import render_template, request, current_app, redirect, url_for, session
from flask_login import login_required, current_user
from apps.audit_mixin import get_remote_addr, check_user_category

@blueprint.route('/pods', methods=["GET"])
@login_required
@check_user_category(["admin"])
def list_pods():
    pods = []
    try:
        pods = k8s.list_pods()
    except Exception as exc:
        current_app.logger.error(f"Failed to list pods: {exc}")

    return render_template("pages/k8s_pod_list.html", pods=pods)


@blueprint.route('/deployments', methods=["GET"])
@login_required
@check_user_category(["admin"])
def list_deployments():
    deployments = []
    try:
        deployments = k8s.list_deployments()
    except Exception as exc:
        current_app.logger.error(f"Failed to list deployments: {exc}")

    return render_template("pages/k8s_dep_list.html", deployments=deployments)


@blueprint.route('/services', methods=["GET"])
@login_required
@check_user_category(["admin"])
def list_services():
    services = []
    try:
        services = k8s.list_services()
    except Exception as exc:
        current_app.logger.error(f"Failed to list services: {exc}")

    return render_template("pages/k8s_srv_list.html", services=services)
