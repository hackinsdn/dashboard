# -*- encoding: utf-8 -*-
"""Support chat thread lifecycle helpers.

A "thread" is one support conversation. Each user may have multiple threads over
time. A thread stays ``open`` until the user or the support team explicitly
finishes it; the next user message after that starts a brand new thread.
"""

from datetime import timedelta

from flask import current_app
from flask_babel import gettext as _
from sqlalchemy import desc

from apps import db
from apps.audit_mixin import utcnow
from apps.controllers import rag_client
from apps.home.models import SupportThreads, SupportMessages


FINISH_MESSAGES = {
    "user": "Conversation finished by the user.",
    "support": "Conversation finished by the support team.",
}

# System notes are staff-facing records of what happened and are stored in
# English, like FINISH_MESSAGES. User-facing prose (assistant answers, refusals)
# goes through gettext instead.
ESCALATE_MESSAGE = "Conversation handed over to the support team."

MODE_SUPPORT = "support"
MODE_ASSISTANT = "assistant"


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


def get_or_create_active_thread(user, mode=None, locale=None):
    """Return the user's active thread, creating a new open one if needed.

    ``mode``/``locale`` apply only to a thread that is actually created: an
    ongoing conversation keeps the mode the user picked when it started.
    """
    thread = get_active_thread(user)
    if thread is None:
        thread = SupportThreads(
            user_id=user.id,
            status="open",
            mode=MODE_ASSISTANT if mode == MODE_ASSISTANT else MODE_SUPPORT,
            locale=locale,
        )
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
    """Number of open support cases awaiting staff; feeds the sidebar badge.

    Assistant conversations are not staff work until the user escalates them
    (which flips ``mode`` to "support"), so they are excluded -- otherwise every
    question asked to the bot would page the support team.
    """
    return SupportThreads.query.filter_by(status="open", mode=MODE_SUPPORT).count()


def generate_support_reply(thread, body):
    """Return an automatic reply for a user message, or None.

    Kept as the synchronous seam for human-mode threads. The RAG assistant does
    **not** run here: on a CPU-only host an answer takes seconds, so it is
    produced by a separate request (see ``answer_with_assistant``) rather than
    holding up the message POST -- that way the user's message is persisted even
    when generation later fails.
    """
    return None


# --- assistant mode ---------------------------------------------------------
def set_thread_mode(thread, mode, locale=None):
    """Set who answers this conversation. Commit by the caller."""
    thread.mode = MODE_ASSISTANT if mode == MODE_ASSISTANT else MODE_SUPPORT
    if locale:
        thread.locale = locale
    return thread


def assistant_available():
    """True when the assistant is enabled, configured and not circuit-broken."""
    return rag_client.is_available()


def assistant_stats(thread):
    """Counters describing how the assistant did in this thread."""
    questions = refusals = negative = 0
    for message in thread.messages:
        if message.sender == "user":
            questions += 1
        elif message.sender == "assistant":
            if message.meta_dict.get("status") == "refused":
                refusals += 1
            if message.feedback == "down":
                negative += 1
    return {"questions_asked": questions, "refusals": refusals, "negative_feedback": negative}


def _history_for(thread, turns):
    """The last ``turns`` user/assistant exchanges, oldest first."""
    if turns <= 0:
        return []
    usable = [m for m in thread.messages if m.sender in ("user", "assistant")]
    # each turn is a user message plus (maybe) its answer; drop the message we
    # are about to answer, which the caller has already persisted
    return [
        {"role": "assistant" if m.sender == "assistant" else "user", "content": m.body}
        for m in usable[:-1][-(turns * 2):]
    ]


def rate_limit_exceeded(user):
    """True when the user has spent their question budget for the window.

    Counted in the database rather than a cache so the limit holds across
    workers and restarts -- the single generation slot is a shared resource and
    one user must not be able to hold it indefinitely.
    """
    limit = current_app.config.get("RAG_USER_RATE_LIMIT", 10)
    window = current_app.config.get("RAG_USER_RATE_WINDOW_MIN", 10)
    if not limit:
        return False
    since = utcnow().replace(tzinfo=None) - timedelta(minutes=window)
    used = (
        db.session.query(SupportMessages)
        .join(SupportThreads, SupportThreads.id == SupportMessages.thread_id)
        .filter(
            SupportThreads.user_id == user.id,
            SupportMessages.sender == "assistant",
            SupportMessages.created_at >= since,
        )
        .count()
    )
    return used >= limit


def _refusal_body():
    return _(
        "I could not find an answer to that in the HackInSDN documentation. "
        "I only answer from the documentation, so I would rather say nothing than guess. "
        "Would you like to talk to a human?"
    )


def _fallback_body(reason):
    if reason == "queue_full":
        return _(
            "I am answering someone else right now. Please try again in a moment, "
            "or open a support case and our team will help you."
        )
    if reason == "timeout":
        return _(
            "That took me too long to answer. Please try again, or open a support case "
            "and our team will help you."
        )
    return _(
        "The assistant is unavailable right now. Please open a support case and "
        "our team will help you."
    )


def answer_with_assistant(thread, question, locale="en"):
    """Ask the assistant and persist its reply. Returns the message, or None.

    Never raises: a refusal and an outage both become an ``assistant`` message
    the user can read and act on. Returns None only when transcripts are
    disabled (RAG_STORE_TRANSCRIPTS=False), where the caller returns the text
    for display without storing it.
    """
    result = rag_client.answer(
        question,
        locale=locale,
        history=_history_for(thread, current_app.config.get("RAG_HISTORY_TURNS", 2)),
        thread_id=thread.id,
    )

    if result.answered:
        body = result.answer
        meta = {
            "status": "answered",
            "sources": result.sources,
            "cached": result.cached,
            "latency_ms": result.usage.get("latency_ms"),
        }
    elif result.status == "refused":
        body = _refusal_body()
        meta = {"status": "refused", "reason": result.reason, "sources": []}
    else:
        body = _fallback_body(result.reason)
        meta = {"status": "unavailable", "reason": result.reason, "sources": []}

    if not current_app.config.get("RAG_STORE_TRANSCRIPTS", True):
        # Nothing is persisted, so there is also nothing to attach feedback to;
        # the widget hides the vote buttons in this mode.
        return None, body, meta

    message = add_message(thread, "assistant", body, is_read=True)
    message.set_meta(meta)
    return message, body, meta


def record_feedback(message, vote, reason=None):
    """Record 👍/👎 on an assistant answer. Re-voting the same way clears it.

    Returns the stored vote (None when cleared). Commit by the caller.
    """
    if vote not in ("up", "down"):
        raise ValueError(f"invalid vote: {vote!r}")
    if message.feedback == vote:
        message.feedback = None
        message.feedback_reason = None
        message.feedback_at = None
        return None
    message.feedback = vote
    message.feedback_reason = (reason or None) if vote == "down" else None
    message.feedback_at = utcnow()
    return message.feedback


def escalate_thread(thread, page=None, user_agent=None, ip=None):
    """Hand an assistant conversation over to human support.

    Captures telemetry **at the moment of the hand-over**: the thread may have
    started on another page, several questions ago, and the page the user gave
    up on is the one staff needs. The snapshot rides on the system message's
    meta, next to the point in the transcript where it applies; the thread's own
    (start-of-conversation) telemetry is left alone, and only backfilled when it
    was never captured.
    """
    if thread.mode != MODE_ASSISTANT:
        return None
    thread.mode = MODE_SUPPORT

    snapshot = {
        "kind": "escalation",
        "page": page or None,
        "user_agent": user_agent or None,
        "ip_address": ip or None,
        "locale": thread.locale,
        "assistant_available": assistant_available(),
    }
    snapshot.update(assistant_stats(thread))
    last_question = next(
        (m.body for m in reversed(thread.messages) if m.sender == "user"), None
    )
    snapshot["last_question"] = last_question

    message = add_message(thread, "system", ESCALATE_MESSAGE, is_read=True)
    message.set_meta(snapshot)

    # A thread that never went through the create-with-telemetry path would show
    # a blank telemetry block to staff; fill it in, without overwriting the
    # original conversation start.
    if not thread.origin_page and page:
        thread.origin_page = page
    if not thread.user_agent and user_agent:
        thread.user_agent = user_agent
    if not thread.ip_address and ip:
        thread.ip_address = ip
    return message


def feedback_summary(days=30, recent=10):
    """Aggregate answer feedback for the admin panel.

    Refusal rate and thumbs-down rate answer different questions -- a thin
    corpus versus bad retrieval/generation -- so both are reported, next to the
    down-voted answers themselves, which are the seed of a regression set.
    """
    since = utcnow().replace(tzinfo=None) - timedelta(days=days)
    answers = (
        SupportMessages.query.filter(
            SupportMessages.sender == "assistant",
            SupportMessages.created_at >= since,
        )
        .order_by(desc(SupportMessages.created_at))
        .all()
    )
    up = sum(1 for m in answers if m.feedback == "up")
    down = sum(1 for m in answers if m.feedback == "down")
    refused = sum(1 for m in answers if m.meta_dict.get("status") == "refused")
    unavailable = sum(1 for m in answers if m.meta_dict.get("status") == "unavailable")
    answered = len(answers) - refused - unavailable
    voted = up + down

    def _question_for(message):
        """The user message this answer replied to."""
        previous = None
        for other in message.thread.messages:
            if other.id == message.id:
                break
            if other.sender == "user":
                previous = other
        return previous.body if previous else None

    return {
        "window_days": days,
        "total": len(answers),
        "answered": answered,
        "refused": refused,
        "unavailable": unavailable,
        "refusal_rate": round(refused / len(answers), 4) if answers else 0.0,
        "up": up,
        "down": down,
        "down_rate": round(down / voted, 4) if voted else 0.0,
        "recent_negative": [
            {
                "message_id": m.id,
                "thread_id": m.thread_id,
                "created_at": f"{m.created_at.isoformat()}Z" if m.created_at else None,
                "reason": m.feedback_reason,
                "question": _question_for(m),
                "answer": m.body,
                "sources": m.sources,
            }
            for m in answers
            if m.feedback == "down"
        ][:recent],
    }


def escalation_snapshot(thread):
    """The most recent escalation telemetry for a thread, or None."""
    for message in reversed(thread.messages):
        if message.sender == "system":
            meta = message.meta_dict
            if meta.get("kind") == "escalation":
                return meta
    return None
