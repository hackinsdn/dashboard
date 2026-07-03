# -*- encoding: utf-8 -*-
"""Support chat thread lifecycle helpers.

A "thread" is one support conversation. Each user may have multiple threads over
time. A thread is considered active while it is ``open`` and the user has
interacted with it within ``SUPPORT_THREAD_INACTIVITY_HOURS``. Once that window
lapses, or the user explicitly finishes it, the thread is ``finished`` and the
next user message starts a brand new thread.
"""

from datetime import timedelta

from flask import current_app
from sqlalchemy import desc

from apps import db
from apps.audit_mixin import utcnow
from apps.home.models import SupportThreads, SupportMessages


def _inactivity_delta():
    hours = current_app.config.get("SUPPORT_THREAD_INACTIVITY_HOURS", 2)
    return timedelta(hours=hours)


def _aware(dt):
    """Return a timezone-aware datetime (DB may return naive UTC values)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=utcnow().tzinfo)
    return dt


def finish_thread(thread):
    """Mark a thread as finished. Caller is responsible for committing."""
    thread.status = "finished"
    thread.finished_at = utcnow()
    return thread


def get_active_thread(user):
    """Return the user's active (open, non-stale) thread, or None.

    If the latest open thread has been inactive for longer than the configured
    window, it is auto-finished and None is returned so a new thread can start.
    """
    thread = (
        SupportThreads.query.filter_by(user_id=user.id, status="open")
        .order_by(desc(SupportThreads.updated_at))
        .first()
    )
    if thread is None:
        return None

    last_activity = _aware(thread.updated_at) or _aware(thread.created_at)
    if last_activity is not None and utcnow() - last_activity > _inactivity_delta():
        finish_thread(thread)
        db.session.commit()
        return None
    return thread


def get_or_create_active_thread(user):
    """Return the user's active thread, creating a new open one if needed."""
    thread = get_active_thread(user)
    if thread is None:
        thread = SupportThreads(user_id=user.id, status="open")
        db.session.add(thread)
        db.session.flush()  # assign an id without a full commit
    return thread


def add_message(thread, sender, body, is_read=False):
    """Append a message to a thread and bump the thread's last-activity time."""
    message = SupportMessages(
        thread_id=thread.id, sender=sender, body=body, is_read=is_read
    )
    db.session.add(message)
    # touch the thread so ``updated_at`` reflects the latest activity
    thread.updated_at = utcnow()
    return message


def mark_thread_read(thread):
    """Mark all user messages in the thread as read (staff has seen them)."""
    changed = False
    for message in thread.messages:
        if message.sender == "user" and not message.is_read:
            message.is_read = True
            changed = True
    return changed


def generate_support_reply(thread, body):
    """Return an automatic reply for a user message, or None.

    This is the single seam for a future AI assistant: return reply text here and
    the API endpoint will persist it as a ``sender="assistant"`` message. Today
    replies come from human staff, so this returns None (no auto-reply).
    """
    # TODO: return an AI assistant reply here to enable automatic answers.
    return None
