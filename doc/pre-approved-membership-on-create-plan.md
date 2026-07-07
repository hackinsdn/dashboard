# Plan (alternative B): apply pre-approved group membership at user creation + CLI sweep

> **Status: implemented** — listener in `apps/authentication/models.py`,
> helper in `apps/utils.py`, CLI in `apps/cli/routes.py`, tests in
> `tests/test_pre_approved_membership.py`, ops docs in `doc/DEV.md`.

Alternative to [check-pre-approved-reorder-plan.md](check-pre-approved-reorder-plan.md)
(alternative A, which reorders `check_pre_approved` itself). Here
**`check_pre_approved()` stays exactly as it is**; instead:

1. when a new user row is inserted (LTI, OAuth or local registration), an
   opportunistic hook adds them to every group whose pre-approved list
   contains their e-mail;
2. a CLI command sweeps *all existing* users against the pre-approved
   lists, for backfill and for lists edited after the users already
   existed.

## Where users are created (all paths must be covered)

| Path | Site |
|---|---|
| LTI launch | `apps/lti/routes.py:235` (`get_or_create_lti_user`) |
| OAuth callback | `apps/authentication/routes.py:110` |
| Local register, no mail server | `apps/authentication/routes.py:164` |
| Local register, after e-mail confirmation | `apps/authentication/routes.py:215` |
| Bootstrap admin | `dbinit.py` |

Rather than editing every site (and forgetting future ones), hook the
model: the codebase already does exactly this for the *Everybody* group —
`add_user_to_sysgrp_everybody`, an `after_insert` listener on `Users` in
`apps/authentication/models.py` that queries `Groups` and inserts
`group_members` rows through the flush `connection`. We add a second
listener right next to it, reusing the proven pattern.

## Changes

### 1. Shared matcher — `apps/utils.py`

```python
def find_pre_approved_groups(email):
    """Groups (not deleted, non-empty approved list) whose pre-approved
    list contains this e-mail. Shared by the Users after_insert listener,
    the CLI sweep and (unchanged) check_pre_approved semantics."""
    if not email:
        return []
    groups = Groups.query.filter(
        Groups.is_deleted == False, Groups.approved_users != "").all()
    return [g for g in groups if email in g.approved_users_list]
```

(`check_pre_approved` itself is *not* refactored to use it — it remains
untouched per this plan; the helper just prevents the listener and the
CLI from duplicating the matching rule. A later no-op refactor may unify
them.)

### 2. `after_insert` listener — `apps/authentication/models.py`

```python
@event.listens_for(Users, 'after_insert')
def add_user_to_pre_approved_groups(mapper, connection, user):
    if not user.email:
        return
    for group in find_pre_approved_groups(user.email):
        if group.organization == "SYSTEM":
            continue  # Everybody is handled by the listener above
        connection.execute(
            insert(group_members),
            dict(user_id=user.id, group_id=group.id),
        )
```

Notes:

- **Raw `connection.execute` inserts, not `group.members.append(user)`** —
  inside a flush event the ORM collections must not be mutated; the
  existing Everybody listener establishes both the constraint and the
  solution.
- No duplicate guard needed at insert time: the row is brand new, its only
  possible membership so far is Everybody (excluded via the SYSTEM skip).
- **Category is intentionally not touched here** (no session writes inside
  a flush event). Promotion still happens through the unchanged
  `check_pre_approved` at the *same login*: all three flows call it after
  the user is added — the membership rows land on the same
  connection/transaction, so `user.member_of_groups` lazy-loads them and
  the existing "member of a non-SYSTEM group → student" branch fires.
  Net effect on a brand-new pre-approved user: membership + `student`
  category in one login, same as today, plus it now also works for LTI
  users whose category is then promoted further by `apply_lti_category`.

### 3. CLI sweep — `apps/cli/routes.py`

```
flask cli sync-pre-approved-users [--dry-run] [--promote/--no-promote]
```

For every non-deleted user with an e-mail: `find_pre_approved_groups(
user.email)`, append the user to each matched group they are not already
in (`user not in group.members` guard — mandatory here, unlike the
listener). With `--promote` (default off), additionally set
`category="student"` for affected users whose category is `"user"`
(mirroring `check_pre_approved`, which would do it at their next login
anyway). `--dry-run` prints the would-be changes and rolls back. Prints a
per-user/per-group summary either way; commits once at the end.

Follows the existing `blueprint.cli` command style in
`apps/cli/routes.py` (`notify-expiring-labs` etc.), so it is cron-able
the same way as the other maintenance jobs — recommend documenting a
periodic run in INSTALL/DEV docs for deployments that edit approved
lists frequently.

## What this approach does and doesn't fix

- New users of **any** eventual category get their pre-approved
  memberships at creation (including LTI teachers on first launch).
- Existing users whose e-mail is added to a list later are covered by
  the CLI sweep (or, for `category == "user"` only, by their next login
  via the unchanged `check_pre_approved`).
- **Known gap**: a user created *without* an e-mail (LTI without the
  e-mail claim) misses the listener; when they set it via
  `/email/required`, the confirm flows call `check_pre_approved`, which
  covers them only while `category == "user"`. Rare combination
  (non-`user` category + late e-mail); the CLI sweep is the safety net —
  called out in the CLI docs.
- E-mail matching stays exact/case-sensitive, as in `check_pre_approved`
  (local registration lowercases e-mails; LMS/IdP e-mails arrive as-is).
  Unifying case handling is a separate follow-up.

## Tests

New `tests/test_pre_approved_membership.py` (own module, standard
header/DB isolation):

- **listener**: create a `Users` row (direct `db.session.add` + commit)
  with an e-mail present in one ORG group's approved list → member after
  commit; also present in a second group's list → member of both; e-mail
  in a SYSTEM group's list → not added there; user without e-mail → only
  Everybody.
- **login-flow integration**: fake LTI launch (existing
  `_FakeMessageLaunch` pattern) for a brand-new pre-approved e-mail →
  user ends up group member *and* `student` in one launch (listener +
  unchanged `check_pre_approved` composing).
- **CLI**: seed users (`user`, `student`, `teacher` categories; one
  already a member; one soft-deleted) and approved lists →
  `sync-pre-approved-users` adds only the missing memberships, never
  duplicates, skips deleted users; `--dry-run` changes nothing;
  `--promote` bumps only the `category == "user"` ones.
- Full-suite run as regression net (`check_pre_approved` untouched, so
  its existing tests in `tests/test_utils.py` must pass unmodified —
  that's the point of this alternative).

## Comparison with alternative A (reorder plan)

| | A: reorder `check_pre_approved` | B: creation hook + CLI (this plan) |
|---|---|---|
| Touches hot login path | yes (every login rescans lists) | no (`check_pre_approved` unchanged) |
| Existing users picked up automatically | on next login | only via CLI sweep (or login while `category=="user"`) |
| Behavior-change risk | return-value/promotion semantics reviewed by every caller | near-zero for existing flows; additive |
| Covers users created by future code paths | only where `check_pre_approved` is called | yes (model-level listener) |

## Rollout

Single commit: `apps/utils.py` (helper), `apps/authentication/models.py`
(listener), `apps/cli/routes.py` (command), new test module, short doc
note. No migration. Run `flask cli sync-pre-approved-users` once after
deploy to backfill existing users.
