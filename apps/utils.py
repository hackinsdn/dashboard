# -*- encoding: utf-8 -*-
"""
Copyright (c) 2015 - HackInSDN team
"""
import datetime

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
        if user_id != "all":
            running_labs = running_labs.filter_by(user_id=user_id)
        running_labs = running_labs.count()
        cache.set(f"running_labs-{user_id}", running_labs)
    g.running_labs = running_labs


def format_duration(delta: datetime.timedelta) -> str:
    """
    Enhanced timedelta format duration from kubernetes.utils.duration
    """

    # Short-circuit if we have a zero delta.
    if delta == datetime.timedelta(0):
        return "0s"

    # Check range early.
    if delta < datetime.timedelta(0):
        return "--"

    # After that, do the usual div & mod tree to take seconds and get days 
    # hours, minutes, and seconds from it.
    secs = int(delta.total_seconds())

    output: List[str] = []

    if delta.days >= 2:
        output.append(f"{delta.days}d")
        if delta.days > 6:
            secs = 0
        else:
            secs -= delta.days * 86400

    hours = secs // 3600
    if hours > 0:
        output.append(f"{hours}h")
        if delta.days > 1 or hours > 3:
            secs = 0
        else:
            secs -= hours * 3600

    minutes = secs // 60
    if minutes > 0:
        output.append(f"{minutes}m")
        if minutes > 4:
            secs = 0
        else:
            secs -= minutes * 60

    if secs > 0:
        output.append(f"{secs}s")

    return "".join(output)
