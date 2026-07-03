# -*- encoding: utf-8 -*-
"""Batched support-chat e-mail notifications.

Instead of e-mailing support on every message, a user's messages are grouped and
sent as a single e-mail once the user has been quiet for at least
``SUPPORT_EMAIL_BATCH_MINUTES``. Invoked from cron via the ``flush-support-emails``
CLI command (see apps/cli/routes.py), mirroring apps/cli/lab_schedule.py.
"""
from datetime import timedelta

from flask_mail import Mail, Message
from flask import render_template
from sqlalchemy import func

from apps import db
from apps.audit_mixin import utcnow
from apps.home.models import SupportThreads, SupportMessages
from apps.authentication.models import Users


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=utcnow().tzinfo)
    return dt


def flush_support_emails(app):
    """Send one grouped e-mail per thread for messages the user has stopped adding to."""
    recipient = app.config.get("MAIL_SENDTO")
    if not recipient:
        app.logger.info("flush_support_emails: MAIL_SENDTO not set, skipping")
        return

    quiet_minutes = app.config.get("SUPPORT_EMAIL_BATCH_MINUTES", 10)
    cutoff = utcnow() - timedelta(minutes=quiet_minutes)
    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")

    # threads that have at least one un-e-mailed user message
    thread_ids = [
        row[0]
        for row in db.session.query(SupportMessages.thread_id)
        .filter(SupportMessages.sender == "user", SupportMessages.emailed_at.is_(None))
        .distinct()
        .all()
    ]
    if not thread_ids:
        app.logger.info("flush_support_emails: nothing pending")
        return

    mail = Mail(app)
    sent = 0
    for thread_id in thread_ids:
        thread = db.session.get(SupportThreads, thread_id)
        if thread is None:
            continue
        pending = [
            m for m in thread.messages
            if m.sender == "user" and m.emailed_at is None
        ]
        if not pending:
            continue
        # Wait until the user has gone quiet: newest pending message older than cutoff.
        newest = max(_aware(m.created_at) for m in pending)
        if newest and newest > cutoff:
            continue

        user = thread.user or db.session.get(Users, thread.user_id)
        try:
            msg = Message(
                subject=f"HackInSDN - New support messages from {user.username if user else thread.user_id}",
                sender=sender,
                recipients=[recipient],
                body=_plaintext(thread, user, pending),
                html=render_template(
                    "mail/support_message.html", user=user, thread=thread, messages=pending
                ),
            )
            mail.send(msg)
        except Exception:
            app.logger.exception(f"flush_support_emails: failed to send for thread={thread_id}")
            continue

        now = utcnow()
        for m in pending:
            m.emailed_at = now
        db.session.commit()
        sent += 1

    app.logger.info(f"flush_support_emails: sent {sent} e-mail(s)")
    return sent


def _plaintext(thread, user, messages):
    lines = []
    if user:
        lines.append(f"User: {user.name} ({user.username}, {user.email})")
    lines.append(f"Thread: {thread.id}")
    if thread.origin_page:
        lines.append(f"Started from: {thread.origin_page}")
    if thread.ip_address:
        lines.append(f"IP: {thread.ip_address}")
    if thread.user_agent:
        lines.append(f"Browser: {thread.user_agent}")
    lines.append("")
    for m in messages:
        stamp = m.created_at.isoformat() if m.created_at else ""
        lines.append(f"[{stamp}] {m.body}")
    lines.append("")
    lines.append("--")
    lines.append("Reply from the Support panel in the Dashboard HackInSDN.")
    return "\n".join(lines)
