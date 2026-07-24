# -*- encoding: utf-8 -*-
"""Batched support-chat e-mail notifications.

Instead of e-mailing support on every message, a user's messages are grouped and
sent as a single e-mail once the user has been quiet for at least
``SUPPORT_EMAIL_BATCH_MINUTES``. A case finished after staff already saw its
pending messages in-app is stamped without sending; a finished case with
never-seen messages (e.g. written and closed by the user) is still reported,
immediately, since the conversation is locked. Invoked from cron via the
``flush-support-emails`` CLI command (see apps/cli/routes.py), mirroring
apps/cli/lab_schedule.py.
"""
from datetime import timedelta

from flask_mail import Mail, Message
from flask import render_template
from sqlalchemy import func

from apps import db
from apps.audit_mixin import utcnow
from apps.controllers.support import escalation_snapshot
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
    skipped = 0
    for thread_id in thread_ids:
        thread = db.session.get(SupportThreads, thread_id)
        if thread is None:
            continue
        if (thread.mode or "support") != "support":
            # An assistant conversation is not staff work until the user
            # escalates it (which flips mode to "support"). Leave its messages
            # un-stamped: if it is escalated later, the whole conversation is
            # still reported in one e-mail.
            continue
        pending = [
            m for m in thread.messages
            if m.sender == "user" and m.emailed_at is None
        ]
        if not pending:
            continue
        if thread.status == "finished":
            if all(m.is_read for m in pending):
                # Staff already saw every pending message in-app and the case is
                # closed: nothing to report. Stamp them so later runs don't
                # re-scan the thread.
                now = utcnow()
                for m in pending:
                    m.emailed_at = now
                db.session.commit()
                skipped += 1
                continue
            # A finished case with never-seen messages (e.g. the user wrote and
            # closed it themselves) is still reported — immediately, skipping the
            # quiet window, since the thread is locked and cannot grow anymore.
        else:
            # Flush once the OLDEST un-e-mailed message has waited at least
            # SUPPORT_EMAIL_BATCH_MINUTES. Basing this on the oldest (not the newest)
            # message means an ongoing conversation can no longer defer the notification
            # indefinitely — support is told within ~the batch window of the first
            # unreported message, and all pending messages are still grouped into one e-mail.
            stamps = [_aware(m.created_at) for m in pending if m.created_at]
            if not stamps or min(stamps) > cutoff:
                continue

        user = thread.user or db.session.get(Users, thread.user_id)
        # A case handed over from the assistant carries the telemetry captured at
        # the hand-over plus the assistant's last reply, so staff can triage it
        # from the inbox without opening the dashboard.
        escalation = escalation_snapshot(thread)
        last_answer = next(
            (m for m in reversed(thread.messages) if m.sender == "assistant"), None
        ) if escalation else None
        try:
            msg = Message(
                subject=f"HackInSDN - New support messages from {user.username if user else thread.user_id}",
                sender=sender,
                recipients=[recipient],
                body=_plaintext(thread, user, pending, escalation, last_answer),
                html=render_template(
                    "mail/support_message.html",
                    user=user,
                    thread=thread,
                    messages=pending,
                    escalation=escalation,
                    last_answer=last_answer,
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

    app.logger.info(
        f"flush_support_emails: sent {sent} e-mail(s),"
        f" skipped {skipped} finished thread(s) already seen by staff"
    )
    return sent


def _plaintext(thread, user, messages, escalation=None, last_answer=None):
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
    if escalation:
        lines.append("")
        lines.append("-- Handed over from the assistant --")
        if escalation.get("page"):
            lines.append(f"Escalated from: {escalation['page']}")
        if escalation.get("ip_address"):
            lines.append(f"IP at escalation: {escalation['ip_address']}")
        if escalation.get("user_agent"):
            lines.append(f"Browser at escalation: {escalation['user_agent']}")
        if escalation.get("locale"):
            lines.append(f"Language: {escalation['locale']}")
        lines.append(
            f"Assistant: {escalation.get('questions_asked', 0)} question(s),"
            f" {escalation.get('refusals', 0)} refusal(s),"
            f" {escalation.get('negative_feedback', 0)} negative vote(s)"
        )
        if last_answer is not None:
            lines.append(f"Last assistant reply: {last_answer.body}")
    lines.append("")
    for m in messages:
        stamp = m.created_at.isoformat() if m.created_at else ""
        lines.append(f"[{stamp}] {m.body}")
    lines.append("")
    lines.append("--")
    lines.append("Reply from the Support panel in the Dashboard HackInSDN.")
    return "\n".join(lines)
