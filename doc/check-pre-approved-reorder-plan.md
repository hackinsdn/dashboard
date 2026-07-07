# Plan (alternative A): reorder `check_pre_approved` so group auto-membership always runs

> See also [pre-approved-membership-on-create-plan.md](pre-approved-membership-on-create-plan.md)
> (alternative B), which keeps `check_pre_approved` untouched and instead
> applies memberships at user-creation time plus a CLI sweep.

## Current behavior and its problems

`apps/utils.py:check_pre_approved(user)` runs in this order:

1. `if user.category != "user": return` — early exit for every already
   -approved category.
2. If the user is a member of any non-SYSTEM group → promote to
   `student`, return `True`.
3. Only then: scan groups with a non-empty `approved_users` list and, on
   an e-mail match, append membership + promote to `student`.

Because the approved-list scan is the *last* step, it is unreachable in
two common cases:

- **Non-`user` categories never get auto-memberships.** A teacher, admin,
  labcreator or already-promoted student whose e-mail appears in a
  group's pre-approved list is never added to that group (step 1 exits
  first).
- **Existing membership blocks new ones.** A user already in group A is
  never auto-added to group B whose approved list contains them (step 2
  returns first).

## Call sites (contract to preserve)

| Caller | Uses return value? |
|---|---|
| `apps/authentication/routes.py:56` (local login), `:113` (OAuth callback), `:304`/`:375` (e-mail confirm flows) | no — commit happens afterwards anyway |
| `apps/authentication/routes.py:503` (`/profile/reload`) | **yes** — `if check_pre_approved(...): db.session.commit()` |
| `apps/api/routes.py:415` (join group by token) | **yes** — same commit-if-changed pattern |
| `apps/lti/routes.py:145` (LTI launch, before `apply_lti_category`) | no |

So the return value must keep meaning **"something was mutated and needs
committing"**.

## New implementation

Move the auto-membership scan to the top and derive the promotion from
the (now updated) membership state:

```python
def check_pre_approved(user):
    """Add the user to every group whose pre-approved list contains their
    e-mail (regardless of category or existing memberships), then promote
    category 'user' -> 'student' when they belong to at least one
    non-SYSTEM group. Returns truthy when anything was changed (callers
    use it as a commit-if-changed signal)."""
    changed = False
    if user.email:
        groups = Groups.query.filter(
            Groups.is_deleted == False, Groups.approved_users != "").all()
        for group in groups:
            if user.email in group.approved_users_list and user not in group.members:
                group.members.append(user)
                changed = True
    if user.category == "user":
        if any(g.organization != "SYSTEM" for g in user.member_of_groups):
            user.category = "student"
            changed = True
    return changed
```

Design notes:

- **Duplicate-membership guard is now mandatory**: with the scan running
  for existing members, a bare `group.members.append(user)` would insert
  a duplicate `group_members` row (IntegrityError at commit). The
  `user not in group.members` collection check is preferred over
  `group.is_member(user.id)` because the OAuth callback (and any future
  flow) calls this on a user that is not flushed yet (`user.id is None`,
  which the SQL count would silently mis-answer).
- **One promotion rule instead of two.** After the appends,
  `user.member_of_groups` already reflects the new memberships
  (`back_populates` syncs both sides in-session), so "just added via
  approved list" and "already a member" collapse into the single
  non-SYSTEM-membership check.
- `user.email` may be `None`/empty (LTI accounts before the
  `/email/required` step) — skip the scan entirely.
- LTI ordering is unaffected: `check_pre_approved` may set `student`,
  then `apply_lti_category` may promote to `teacher`, exactly as today.

## Deliberate behavior changes (review these)

1. Teachers/admins/labcreators/students now **gain group memberships**
   from pre-approved lists on every login/launch/reload (the purpose of
   this change). Their category is never touched.
2. Users already in some group also get added to **other** approved
   groups.
3. Edge: an approved-list match on a **SYSTEM-organization** group now
   adds membership but no longer promotes the category (old code promoted
   for any approved-list add). SYSTEM groups ("Everybody") don't carry
   approved lists in practice, and SYSTEM membership already doesn't
   promote in the membership path — this makes the two paths consistent.
4. Return-value nuance: previously "member of non-SYSTEM group but
   category already promoted" returned `None` at step 1; now such calls
   return falsy too unless a membership was added — same commit behavior.
5. Cost: the `Groups` approved-list query now runs for every call, not
   only for `category == "user"` — one small filtered query per
   login/launch; acceptable.

## Tests (`tests/test_utils.py::TestCheckPreApproved`)

Adjust:

- `test_non_user_category_is_ignored` → becomes
  `test_non_user_category_gets_membership_but_keeps_category`: student
  with a matching approved e-mail is **added to the group** (truthy
  return) and category stays `student`.

Keep as-is (behavior unchanged):
`test_member_of_normal_group_is_promoted`,
`test_system_only_group_does_not_promote`,
`test_approved_email_promotes_and_adds_to_group`.

Add:

- member of group A + approved in group B → added to B (truthy), still
  `student`;
- idempotency: calling twice adds nothing the second time and returns
  falsy (guards the duplicate-insert regression);
- user with `email=None` → no crash, falsy, no memberships;
- teacher with matching approved e-mail → membership added, category
  stays `teacher`;
- full-suite run (`pytest tests/`) as the regression net — the LTI launch
  tests exercise `check_pre_approved` on every fake launch.

## Rollout

Single small commit: `apps/utils.py` + `tests/test_utils.py`. No
migration, no caller changes, no config. Can ship inside the LTI branch
(LTI onboarding benefits directly: a teacher's first launch now picks up
their pre-approved course groups) or cherry-picked to `main` on its own.
