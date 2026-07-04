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
- Lab Guide attachments — copied on disk, see below

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
under `UPLOAD_DIR`. Deleting an attachment from a Lab **removes the file
from disk**, so the duplicate must never share filenames with the source:

1. Each source attachment is copied on disk to a fresh
   `uuid4().hex + extension` filename (files missing on disk are skipped
   with a warning).
2. References to the old filename are rewritten to the new one in the
   copied Lab Guide markdown and extended description.
3. The copies are handed to the form as *pending uploads* — the same
   mechanism used when attaching files while creating a brand new Lab — so
   the `edit_lab` POST handler associates them with the new Lab on save.

If the user abandons the form, the copied files remain as orphans on disk;
this is the same exposure the pre-existing new-lab upload flow already has.

Images embedded in the extended description via the rich-text editor are
uploaded without per-lab tracking (shared URLs by design), so they need no
special handling.

## Limitations / future work

- **ContainerLabs**: clabs are edited through the dedicated
  `clabs` upsert page, and duplicating one also requires copying the
  topology stored in `LabMetadata.md` and issuing a new `short_uuid`. The
  Duplicate button is therefore hidden for clab cards (when `ENABLE_CLABS`
  is on); clab duplication is left for a follow-up.
- Answer sheets, schedules and statistics are not copied — the duplicate is
  a fresh Lab with no usage history.

## Tests

`tests/test_labs.py::TestDuplicate` covers: role gating (student denied),
unknown/deleted source rejection, form prefill with `-- Copy` title and all
source values, the guarantee that the GET persists nothing, submitting the
prefilled form as a new Lab, title truncation, labcreator scope (own,
group-shared, unshared-denied), on-disk copy of attachments with guide
rewriting, and Duplicate button visibility on the list page.
