# Lab Field Version Control — Design

## Overview

Editable Lab content used to be overwritten wholesale on every save: only the last
writer/time survived (via `AuditMixin`), so there was no way to see what changed or to
roll back a bad edit. This feature records a full snapshot of each **tracked field**
every time it changes, shows the history (with diffs) in the Lab editor, and lets an
editor restore any previous version.

Tracked fields: **`manifest`**, **`lab_guide`** (markdown source) and
**`extended_desc`**.

## Why database versioning (and not git)

Two approaches were considered:

1. **A version table in the database** (chosen).
2. **Files on disk committed to a git repository** (GitPython is already a dependency,
   used read-only by `apps/controllers/git.py` for lab templates).

The database is the source of truth for lab content at runtime (`run_lab` reads
`Labs.manifest` directly), so git would only ever be a shadow copy, with the usual
dual-write problems: a crash between the DB commit and the git commit silently desyncs
history; the git index needs locking under concurrent editors and breaks down with
multiple app replicas on shared volumes; and deployment/backup would span two stores.
Git's genuine advantages (diff, blame, remotes) matter when files are primary — here
they aren't, and stdlib `difflib` covers the diff need. A DB row written **in the same
transaction as the Lab save** can never diverge from the Lab itself.

## Data model

`LabFieldVersions` (`apps/home/models.py`), migration `2.0.10 → 2.0.11`:

| Column | Notes |
| --- | --- |
| `id` | PK |
| `lab_id` | FK → `labs.id` |
| `field` | `manifest` (default) \| `lab_guide` \| `extended_desc` |
| `version` | per-lab-per-field ascending sequence (unique together with `lab_id`+`field`) |
| `content` | full snapshot of the field at that version |
| `created_at` / `updated_by` | from `AuditMixin`: when and by whom the version was saved |

The migration backfills **version 1** for every existing lab from its current content,
so history starts populated.

## Behavior

- **Capture** (`apps/controllers/lab_versions.py`, called from `edit_lab` in
  `apps/home/routes.py`): on save, each tracked field whose submitted value differs
  from the stored one gets a new version row with the **new** value, added to the same
  session/transaction as the Lab save. Unchanged fields and failed validations record
  nothing. Lab duplication ("fork") goes through the same save path, so the fork gets
  its own version 1.
- **Rollback is editor-based and non-destructive**: the history modal loads an old
  version back into the form; saving the Lab records it as a *new* version through the
  normal permission checks and validation. There is no server-side restore endpoint.
- **Retention**: `LAB_FIELD_VERSIONS_MAX` (default 50, `apps/config.py`) versions are
  kept per lab per field; the oldest are pruned when a new version exceeds the cap.

## HTTP API (`apps/api/routes.py`, `@login_required`)

| Method & path | Purpose |
| --- | --- |
| `GET /api/labs/<lab_id>/field_versions/<field>` | history list (version, saved-at, author), most recent first |
| `GET /api/labs/<lab_id>/field_versions/<field>/<version>` | stored content; `?diff=1` returns a unified diff against the lab's current value |

Access mirrors `edit_lab`: `admin` and `teacher` for any lab, `labcreator` only for
labs they own (`Labs.updated_by`); soft-deleted labs are visible to admins only.

## UI

In `pages/labs_edit.html` each tracked field's card header gets a **history** button
(existing labs only) opening a shared modal that lists versions with *Diff vs current*,
*View* and *Restore into editor* actions.

## Testing

`tests/test_lab_field_versions.py` covers: version 1 on create, per-field capture on
edit, no-op saves and failed validation recording nothing, the history/content/diff
endpoints, permission gating (student denied; labcreator only own labs), the
restore-and-save flow producing a new version, and retention pruning.
