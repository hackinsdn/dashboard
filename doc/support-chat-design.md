# Support Chat — High-Level Design

## Overview

The support chat gives every logged-in user an in-app channel to reach the support
team without leaving the dashboard. A floating button in the bottom-right corner of
every page expands into a small chat window where the user can start a conversation,
send messages, and read replies. Support staff answer from an admin panel, and users
can review their own conversation history from a dedicated page.

The backend is a **human support-ticket** system: replies come from staff. It is
deliberately built around a single extension point (`generate_support_reply`) so the
same UI and transport can later be backed by an **AI assistant** without further
changes to the widget or the endpoints.

This document records the originally requested requirements and the subsequent
refinements, and describes the resulting architecture.

---

## Requirements

### Original requirements (initial feature)

1. A **floating "chat" button** fixed to the bottom-right corner of every page.
2. Clicking it **expands** a small chat window with a message box, where the user can
   send messages and receive replies.
3. Once expanded, a **minimize** button collapses the window back to the button.
4. Messages are persisted and reach the support team; replies come from **staff**
   (architected so an **AI assistant** can be plugged in later).
5. **Multiple threads per user**: sending a message with no active thread starts a new
   thread. A thread ends when the user is **inactive for > 2 h** or **finishes it**
   explicitly.
6. An **admin-only** management page (under the MANAGEMENT sidebar section) listing
   threads that are open or have unread user messages, with the ability to open a
   thread and **reply**.

### Refinements (follow-up)

1. A **user-facing** page to view the user's **own** threads and messages
   (admin-style, but scoped to the current user).
2. A **"See all cases"** link inside the chat widget pointing to that user page.
3. Make the top navbar **Messages dropdown** dynamic: show the user's most recent
   threads and point **"See All Messages"** at the user page.
4. **Batch e-mails**: instead of e-mailing support on every message, group a user's
   messages and send **one** e-mail once they have been quiet for ~10 min.
5. Capture **telemetry** when a conversation starts (current page, browser/user-agent,
   IP) and report it on the support case.
6. The **admin** threads page shows only **open** (not finished) threads by default,
   with a button to toggle **show all** threads.
7. Each chat-widget message shows a small **timestamp** (time of day; plus
   **DD/MM/YYYY** when the message is not from today).
8. When a thread is open for viewing, a JS routine **polls for new messages every
   30 s**.

### Refinements (second follow-up)

9. The admin **sidebar "Support" entry** shows a badge with the number of threads that
   have unread user messages (needs staff attention), similar to "Running Labs".
10. The navbar **chat icon** shows a badge with the number of the current user's threads
    that have an unseen staff reply (already present; distinct from the sidebar count).
11. The chat-widget input supports **multi-line** messages: **Enter** sends, **Shift+Enter**
    inserts a new line; the box auto-grows and newlines are preserved in the bubbles.
12. The widget is **responsive**: on small screens it spans the viewport width with
    symmetric side gutters instead of a fixed panel anchored to the right edge.

### Refinements (third follow-up)

13. Admins can **finish a conversation** from a button on the admin thread-view page.
14. Whenever a conversation is closed (by the user, by an admin, or automatically due to
    inactivity), a **`system` message** is recorded stating who/what closed it, and is
    shown as a centered note in the thread.
15. Multi-line messages are rendered with their **line breaks preserved** in the admin and
    user thread-view pages (`white-space: pre-wrap`).
16. After a conversation is finished, the widget **locks**: it cannot send new messages
    into it (the API rejects with `409`), it shows the closing note and disables the send
    button, and offers a **"Start new conversation"** action.

### Refinements (fourth follow-up)

17. The widget **persists its open/minimized state** in `localStorage`, so it stays open
    across page navigation.
18. The widget is sized and positioned against the **visual viewport**
    (`window.visualViewport`) rather than the layout viewport, so it fits the visible screen
    even on pages whose content overflows horizontally (page overflow inflates the layout
    viewport, which previously made the `vw`/`right`-anchored panel larger than the phone
    screen).

---

## Architecture

### Data model (`apps/home/models.py`)

Two tables, both using `AuditMixin` (`created_at`, `updated_at`, `updated_by`).

**`SupportThreads`** — one support conversation.

| Column | Notes |
| --- | --- |
| `id` | PK |
| `user_id` | FK → `users.id` (thread owner) |
| `status` | `open` \| `finished` |
| `finished_at` | when the thread was closed |
| `origin_page` | telemetry: page the conversation started from |
| `user_agent` | telemetry: browser/User-Agent header |
| `ip_address` | telemetry: remote address |
| `user_last_read_at` | when the owner last viewed the thread (drives the navbar unread badge) |

Helper properties: `unread_count` (user messages staff hasn't seen),
`has_unseen_for_user` (a `support`/`assistant` reply arrived after `user_last_read_at`),
and `as_dict(with_messages=…)`.

**`SupportMessages`** — one message in a thread.

| Column | Notes |
| --- | --- |
| `id` | PK |
| `thread_id` | FK → `support_threads.id` |
| `sender` | `user` \| `support` \| `assistant` \| `system` (closing note) |
| `body` | message text |
| `is_read` | staff-side read flag (user message seen by staff) |
| `emailed_at` | null = not yet included in a batched support e-mail |

Schema is created by migration `2.0.7 → 2.0.8`
(`migrations/versions/2.0.7_add_support_chat_2.0.8.py`).

### Thread lifecycle (`apps/controllers/support.py`)

- `get_active_thread(user)` — latest `open` thread within
  `SUPPORT_THREAD_INACTIVITY_HOURS` (default 2 h). A stale open thread is
  auto-finished and `None` is returned.
- `get_or_create_active_thread(user)` — returns the active thread or creates a new one.
- `add_message(thread, sender, body, is_read)` — append a message and bump the thread's
  last-activity time. User messages start with `emailed_at = NULL`.
- `finish_thread(thread, by="user")` — mark `finished` + set `finished_at`, and record a
  `system` closing message (`FINISH_MESSAGES[by]` for `user` / `support` / `inactivity`).
  Idempotent: a thread already `finished` is left untouched.
- `mark_thread_read(thread)` — staff-side: mark user messages read.
- `mark_thread_seen_by_user(thread)` — user-side: set `user_last_read_at = now`.
- `record_telemetry(thread, page, user_agent, ip)` — set the three telemetry fields
  (only when a thread is created).
- `recent_threads_for_user(user, limit)` / `user_unread_thread_count(user)` — feed the
  navbar dropdown (the user's own unseen staff replies).
- `admin_unread_thread_count()` — number of threads with at least one unread user
  message; feeds the admin sidebar "Support" badge.
- `generate_support_reply(thread, body)` — **AI seam**; returns `None` today, so replies
  come from humans. Returning text here would persist an `assistant` message.

### HTTP API (`apps/api/routes.py`, prefix `/api`, `@login_required`)

| Method & path | Who | Purpose |
| --- | --- | --- |
| `GET /support/thread` | user | active thread + messages (marks it seen by the user) |
| `POST /support/thread/messages` | user | append a user message; record telemetry on a **new** thread; store any AI reply. Accepts an optional `thread_id` — if it names a **finished** thread the request is rejected with `409 {finished: true}`; if omitted it continues/starts the active thread |
| `POST /support/thread/finish` | user | finish the active thread |
| `POST /support/threads/<id>/finish` | admin | finish any thread |
| `GET /support/threads/<id>` | admin **or** owner | single thread + messages (used by the 30 s poll) |
| `POST /support/threads/<id>/messages` | admin | staff reply; marks the thread's user messages read |

Support is **not** e-mailed from the request path; notifications are batched (below).

### Server-rendered pages (`apps/home/routes.py`)

- `GET /support/threads` — **admin only** (`@check_user_category(["admin"])`); default
  lists only `open` threads, `?show=all` lists every thread.
- `GET /support/threads/<id>` — admin thread view + reply form; opening marks user
  messages read.
- `GET /support/my` — the current user's own threads.
- `GET /support/my/<id>` — read-only conversation for one of the user's own threads
  (404 for someone else's); opening marks staff replies seen.
- `@blueprint.app_context_processor inject_support_dropdown` — provides
  `support_recent_threads`, `support_unread_count` (user, for the navbar), and
  `support_admin_unread_count` (admins only, for the sidebar badge) to every page.

### UI components

- **Floating widget** — `apps/templates/includes/chatbot.html`, included from
  `layouts/base.html` for authenticated users (Font Awesome is loaded globally in
  `base.html`). Vanilla JS (no jQuery dependency). Features: expand/minimize, load and
  send messages, **multi-line input** (`<textarea>`: Enter sends, Shift+Enter adds a
  newline, auto-grows; newlines preserved via `white-space: pre-wrap`),
  **per-message timestamps** (`HH:MM`, or `DD/MM/YYYY HH:MM` when not today),
  **"Finish conversation"**, **"See all cases"** link → `/support/my`, sends the current
  `page` for telemetry, and **polls every 30 s** while open. A `@media (max-width: 575.98px)`
  rule makes the panel span the viewport with symmetric gutters on phones. The widget
  tracks its `currentThreadId` and polls it by id; when that thread becomes **finished**
  (by an admin, or on a `409` from a send) it **locks** — disables the input/send button,
  shows the closing note, and offers **"Start new conversation"**. It **persists its
  open/minimized state** in `localStorage` (stays open across navigation) and sizes/positions
  itself against `window.visualViewport` so it fits the visible screen even when the page
  overflows horizontally.
- **Navbar Messages dropdown** — `apps/templates/includes/navigation.html`; shows the
  user's recent threads, an unread badge (`support_unread_count`), and "See All
  Messages" → `/support/my`.
- **Sidebar "Support" entry** — `apps/templates/includes/sidebar.html` (admin only);
  right-aligned badge showing `support_admin_unread_count` when > 0.
- **Admin pages** — `pages/support_threads.html` (list + open/all toggle),
  `pages/support_thread_view.html` (conversation, telemetry block, reply form, **finish
  button**, 30 s poll). Both thread-view pages render `system` messages as centered notes
  and preserve line breaks in multi-line messages (`white-space: pre-wrap`).
- **User pages** — `pages/my_support_threads.html` (list),
  `pages/my_support_thread_view.html` (read-only conversation, 30 s poll).

### Batched e-mail notifications (cron)

Support notifications are grouped and sent by the `flush-support-emails` CLI command
(`apps/cli/support_notify.py`, registered in `apps/cli/routes.py`), triggered by cron —
see [DEV.md](./DEV.md#scheduled-jobs-cron). It finds threads with un-e-mailed user
messages whose newest message is older than `SUPPORT_EMAIL_BATCH_MINUTES` (default 10),
sends **one** e-mail per thread to `MAIL_SENDTO` (including telemetry), and stamps the
messages `emailed_at`. The e-mail body is `templates/mail/support_message.html`.

### Configuration (`apps/config.py`)

| Setting | Default | Meaning |
| --- | --- | --- |
| `SUPPORT_THREAD_INACTIVITY_HOURS` | `2` | idle time before an open thread auto-finishes |
| `SUPPORT_EMAIL_BATCH_MINUTES` | `10` | quiet time before pending messages are e-mailed |
| `MAIL_SENDTO` | — | support inbox; batched e-mails are skipped if unset |

### Permissions summary

- The widget, user pages, and the navbar dropdown are available to any authenticated
  user, always scoped to their own threads.
- The admin list/detail pages and the staff-reply endpoint require the `admin` category.
- The read endpoint `GET /api/support/threads/<id>` is allowed for the thread owner or
  an admin, and returns `403` otherwise.

---

## Testing

`tests/test_support_chat.py` covers the widget flow (start/continue/finish, the 2 h
boundary, per-user scoping), admin management (role gating, reply + mark-read, open-vs-all
filter), telemetry capture on a new thread, the read-endpoint authorization, the navbar
unread indicator (including the rendered badge), the admin sidebar unread count
(`admin_unread_thread_count`, rising with a pending message and dropping after a reply),
multi-line message persistence, conversation finishing (user + admin, the closing `system`
message, admin-route authorization, and idempotency), posting into a specific thread
(append vs. the `409` rejection on a finished thread, and ownership 404s), and the
batch-flush timing boundary (with a mocked mailer).

## Future work

- Wire `generate_support_reply` to an AI assistant to provide instant answers; the UI,
  endpoints, and storage already support an `assistant` sender.
- Optional: notify the **user** by e-mail when staff reply (currently in-app only).
