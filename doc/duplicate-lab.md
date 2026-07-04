# Duplicate (Fork) a Lab

This document describes the "Duplicate Lab" feature: how it behaves for
users, which permission rules apply, and how it is implemented.

## Overview

Duplicating a Lab opens the regular Lab creation page with every field
pre-filled from an existing Lab, so the user can tweak and submit it as a
brand new Lab. The pre-filled title receives a ` -- Copy` suffix (e.g.
`Intro to SDN` becomes `Intro to SDN -- Copy`). Nothing is persisted until
the user presses **Submit** — abandoning the page leaves the catalog
unchanged.

## Where the action is available

- **Labs list page** (`/labs/view`): each Lab card footer shows a
  **Duplicate** button (copy icon) next to Update, for admins, teachers and
  labcreators. The button is hidden for deleted labs and for ContainerLabs
  (see Limitations).
- **Lab edit page** (`/labs/edit/<lab_id>`): a **Duplicate** button next to
  Submit/Cancel, shown when editing an existing, non-deleted Lab.

As part of this change the Labs list card buttons were shortened to fit the
extra action: `View Lab` → `View`, `Start Lab` → `Start`, `Resume Lab` →
`Resume`, `Update Lab`/`Update ContainerLab` → `Update`, `Restore Lab` →
`Restore` and `Delete Lab` → `Delete`. Confirmation modals keep the full
wording.

## Permissions

The route is `GET /labs/duplicate/<lab_id>` and is restricted to the
`admin`, `teacher` and `labcreator` categories:

| Role | May duplicate |
| --- | --- |
| admin | any non-deleted Lab |
| teacher | any non-deleted Lab (mirrors their unrestricted edit access) |
| labcreator | any Lab they can view: shared with one of their groups, or their own |

The labcreator rule intentionally reuses the same visibility predicate as
the Labs catalog (`view_labs`): a labcreator may *fork* any Lab they can
see, even though they may only *edit* Labs they own. Labs outside that
predicate (and unknown or soft-deleted ids) answer with the same "Lab not
found" page used elsewhere, so the route does not leak Lab existence.

## What gets copied

- Title (with the ` -- Copy` suffix, truncated so it fits the 255-character
  column limit)
- Short description
- Categories
- Allowed groups (access control)
- Extended description
- Lab Guide (markdown)
- Kubernetes manifest
- Goals
- Lab Guide attachments — shared with the source lab, see below

The audit log (`HomeLogging`) records a `duplicate_lab` action with the
*source* Lab id when the form is opened; saving the copy goes through the
normal create flow and is logged as `edit_lab` like any other new Lab.

## Implementation notes

The feature reuses the existing create/edit machinery instead of adding a
separate save path:

- `duplicate_lab` (in `apps/home/routes.py`) loads the source Lab and
  renders the existing `pages/labs_edit.html` template in **"new lab"
  mode**: the pre-filled object has `id=None`, so the form posts to
  `/labs/edit/new` and the standard `edit_lab` POST handler creates the new
  Lab with its own validation, logging and redirect behavior.
- The prefill is a plain `SimpleNamespace`, **not** a `Labs` model
  instance. Appending persistent categories/groups to a transient
  SQLAlchemy object could pull it into the session via backref cascade and
  accidentally persist a half-built Lab on autoflush; a namespace object
  renders identically in the template with zero session side effects.
- An informational alert on the form tells the user which Lab is being
  duplicated and that nothing is saved until Submit.

### Guide attachments (uploads)

Lab Guide attachments are tracked in `LabMetadata.md["uploads"]` and stored
under `UPLOAD_DIR`. The duplicate **shares** the source's files instead of
copying them — the duplicate GET writes nothing to disk, so abandoning the
form leaves no orphan files behind:

1. The source's uploads list is handed to the form as *pending uploads* —
   the same mechanism used when attaching files while creating a brand new
   Lab. The pre-filled guide keeps its original `/uploads/<filename>`
   references, which stay valid.
2. On save, the `edit_lab` POST handler associates the same filenames with
   the new Lab's `LabMetadata`; both Labs now reference one file on disk.
3. Attachment deletion (`DELETE /labs/<id>/uploads/<filename>`) is
   **reference-counted**: the entry is always removed from the requesting
   Lab's uploads list, but the file is only removed from disk when no other
   Lab's metadata still references that filename. This holds for arbitrary
   duplicate chains — the last Lab holding a reference deletes the file.
   The check is a substring match on the metadata JSON, which is precise
   because uploaded filenames are unique `uuid4().hex` values.

Files are immutable after upload (there is no edit-in-place), so sharing
them between Labs is safe, and `serve_upload` has no per-lab access
control that sharing could bypass.

Images embedded in the extended description via the rich-text editor
(summernote) are registered in `LabMetadata.md["uploads"]` exactly like
guide attachments, so they follow the same sharing and reference-counted
deletion rules. The "remove attachment" button refuses to delete a file
that is still referenced in either the Lab Guide or the extended
description. (Images uploaded before this tracking existed remain
untracked shared URLs.)

## Limitations / future work

- **ContainerLabs**: clabs are edited through the dedicated
  `clabs` upsert page, and duplicating one also requires copying the
  topology stored in `LabMetadata.md` and issuing a new `short_uuid`. The
  Duplicate button is therefore hidden for clab cards (when `ENABLE_CLABS`
  is on); clab duplication is left for a follow-up.
- Answer sheets, schedules and statistics are not copied — the duplicate is
  a fresh Lab with no usage history.
- Uploading a file while creating a brand-new Lab and then abandoning the
  form still orphans that file (pre-existing behavior, unrelated to
  duplication). A follow-up sweeper could delete `UPLOAD_DIR` files older
  than N days that no Lab metadata or content references.

## Tests

`tests/test_labs.py::TestDuplicate` covers: role gating (student denied),
unknown/deleted source rejection, form prefill with `-- Copy` title and all
source values, the guarantee that the GET persists nothing to the database
and writes nothing to disk, submitting the prefilled form as a new Lab,
title truncation, labcreator scope (own, group-shared, unshared-denied),
attachment sharing on save, reference-counted attachment deletion, and
Duplicate button visibility on the list page.
