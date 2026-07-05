# -*- encoding: utf-8 -*-
"""Support chat thread lifecycle helpers.

A "thread" is one support conversation. Each user may have multiple threads over
time. A thread stays ``open`` until the user or the support team explicitly
finishes it; the next user message after that starts a brand new thread.
"""

from sqlalchemy import desc

from apps import db
from apps.audit_mixin import utcnow
from apps.home.models import SupportThreads, SupportMessages


FINISH_MESSAGES = {
    "user": "Conversation finished by the user.",
    "support": "Conversation finished by the support team.",
}


def finish_thread(thread, by="user"):
    """Mark a thread finished and record a closing system message.

    When the support team finishes a case, its user messages are marked read:
    closing it implies staff has handled them (this also keeps the batched
    e-mail job from reporting a case staff already dealt with).

    Idempotent: a thread that is already finished is left untouched (no duplicate
    system message). Caller is responsible for committing.
    """
    if thread.status == "finished":
        return thread
    thread.status = "finished"
    thread.finished_at = utcnow()
    if by == "support":
        mark_thread_read(thread)
    add_message(thread, "system", FINISH_MESSAGES.get(by, FINISH_MESSAGES["user"]), is_read=True)
    return thread


def get_active_thread(user):
    """Return the user's active (open) thread, or None.

    Threads stay open until explicitly finished by the user or the support team.
    """
    return (
        SupportThreads.query.filter_by(user_id=user.id, status="open")
        .order_by(desc(SupportThreads.updated_at))
        .first()
    )


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


def mark_thread_seen_by_user(thread):
    """Record that the owning user has just viewed this thread. Commit by caller."""
    thread.user_last_read_at = utcnow()
    return thread


def record_telemetry(thread, page=None, user_agent=None, ip=None):
    """Store where/how a conversation started. Called only when a thread is created."""
    thread.origin_page = page
    thread.user_agent = user_agent
    thread.ip_address = ip
    return thread


def recent_threads_for_user(user, limit=5):
    """Most recently active threads owned by the user (for the navbar dropdown)."""
    return (
        SupportThreads.query.filter_by(user_id=user.id)
        .order_by(desc(SupportThreads.updated_at))
        .limit(limit)
        .all()
    )


def user_unread_thread_count(user):
    """Number of the user's threads with a staff/assistant reply they haven't seen."""
    threads = SupportThreads.query.filter_by(user_id=user.id).all()
    return sum(1 for t in threads if t.has_unseen_for_user)


def open_thread_count():
    """Number of open support cases; feeds the admin sidebar "Support" badge."""
    return SupportThreads.query.filter_by(status="open").count()


def generate_support_reply(thread, body):
    """Return an automatic reply for a user message, or None.

    This is the single seam for a future AI assistant: return reply text here and
    the API endpoint will persist it as a ``sender="assistant"`` message. Today
    replies come from human staff, so this returns None (no auto-reply).
    """
    # TODO: return an AI assistant reply here to enable automatic answers.
    return None
