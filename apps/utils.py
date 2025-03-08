# -*- encoding: utf-8 -*-
"""
Copyright (c) 2015 - HackInSDN team
"""
from flask import g
from flask_login import current_user

from apps import cache
from apps.home.models import LabInstances


def update_running_labs_stats():
    """Update statistics on running labs and make them available on Flask 'g' variable,
    so that it can be easily accessed from Jinja template"""
    user_id = current_user.id if hasattr(current_user, "id") else "all"
    running_labs = cache.get(f"running_labs-{user_id}")
    if running_labs is None:
        running_labs = LabInstances.query.filter_by(is_deleted=False)
        if user_id != "all" and getattr(current_user, "category", "student") not in ["admin", "teacher"]:
            running_labs = running_labs.filter_by(user_id=user_id)
        running_labs = running_labs.count()
        cache.set(f"running_labs-{user_id}", running_labs)
    g.running_labs = running_labs
