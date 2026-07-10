# -*- encoding: utf-8 -*-
"""Version control for editable Lab fields.

Every time one of the tracked fields (``manifest``, ``lab_guide``,
``extended_desc``) changes on save, a full snapshot of the new value is
recorded in ``lab_field_versions`` inside the same transaction as the Lab
save, so history can never diverge from the Lab itself. Rollback is done
through the editor: an old version is loaded back into the form and saved
normally, which records it as a new version (non-destructive).
"""

import difflib

from flask import current_app

from apps import db
from apps.home.models import LabFieldVersions

TRACKED_FIELDS = ("manifest", "lab_guide", "extended_desc")


def current_value(lab, field):
    """Return the lab's current value for a tracked field as str ('' if unset)."""
    if field == "manifest":
        return lab.manifest or ""
    if field == "lab_guide":
        return lab.lab_guide_md.decode() if lab.lab_guide_md else ""
    if field == "extended_desc":
        return lab.extended_desc.decode() if lab.extended_desc else ""
    raise ValueError(f"Untracked lab field: {field}")


def record_version(lab, field, content):
    """Append the next version row for a field and prune the oldest beyond the cap."""
    if field not in TRACKED_FIELDS:
        raise ValueError(f"Untracked lab field: {field}")
    last = (
        LabFieldVersions.query.filter_by(lab_id=lab.id, field=field)
        .order_by(LabFieldVersions.version.desc())
        .first()
        if lab.id
        else None
    )
    next_version = (last.version + 1) if last else 1
    row = LabFieldVersions(lab=lab, field=field, version=next_version, content=content)
    db.session.add(row)

    max_versions = current_app.config.get("LAB_FIELD_VERSIONS_MAX", 50)
    if next_version > max_versions:
        (
            LabFieldVersions.query.filter(
                LabFieldVersions.lab_id == lab.id,
                LabFieldVersions.field == field,
                LabFieldVersions.version <= next_version - max_versions,
            ).delete(synchronize_session=False)
        )
    return row


def list_versions(lab, field):
    """All versions of a field for a lab, most recent first."""
    return (
        LabFieldVersions.query.filter_by(lab_id=lab.id, field=field)
        .order_by(LabFieldVersions.version.desc())
        .all()
    )


def get_version(lab, field, version):
    return LabFieldVersions.query.filter_by(
        lab_id=lab.id, field=field, version=version
    ).first()


def delete_version(lab, field, version):
    """Remove a single stored version. Returns True when a row was deleted."""
    row = get_version(lab, field, version)
    if not row:
        return False
    db.session.delete(row)
    return True


def diff_against_current(lab, version_row):
    """Unified diff from the stored version to the lab's current field value."""
    old = (version_row.content or "").splitlines()
    new = current_value(lab, version_row.field).splitlines()
    return "\n".join(
        difflib.unified_diff(
            old,
            new,
            fromfile=f"{version_row.field} v{version_row.version}",
            tofile=f"{version_row.field} (current)",
            lineterm="",
        )
    )
