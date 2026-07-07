# -*- encoding: utf-8 -*-
"""HackInSDN"""

import click
from flask import current_app
from apps import db
from apps.cli import blueprint
from apps.cli.lab_schedule import alert_expiring_labs, run_delete_expired_labs
from apps.cli.support_notify import flush_support_emails
from apps.authentication.models import Users
from apps.utils import find_pre_approved_groups

@blueprint.cli.command('notify-expiring-labs')
@click.option(
    "--send-email/--no-send-email",
    default=False,
    help="Notify users via e-mail about expiring labs",
)
def notify_expiring_labs(send_email):
    """List expiring Lab Instances and notify users by e-mail (if enabled)"""
    alert_expiring_labs(current_app, send_email=send_email)


@blueprint.cli.command('remove-expired-labs')
def remove_expired_labs():
    """Remove expired Lab Instances"""
    run_delete_expired_labs(current_app)


@blueprint.cli.command('flush-support-emails')
def flush_support_emails_cmd():
    """Send batched support-chat e-mails for users who have gone quiet"""
    flush_support_emails(current_app)


@blueprint.cli.command('sync-pre-approved-users')
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Only print what would change, without saving",
)
@click.option(
    "--promote/--no-promote",
    default=False,
    help="Also promote category 'user' to 'student' for users that got new memberships",
)
def sync_pre_approved_users(dry_run, promote):
    """Add existing users to groups whose pre-approved list contains their
    e-mail (backfill for the automatic membership applied at user creation)."""
    changed_users = 0
    users = Users.query.filter(
        Users.is_deleted == False, Users.email != None, Users.email != ""
    ).all()
    for user in users:
        added = []
        for group in find_pre_approved_groups(user.email):
            if group.organization == "SYSTEM":
                continue
            if user in group.members:
                continue
            group.members.append(user)
            added.append(group.groupname)
        if not added:
            continue
        promoted = False
        if promote and user.category == "user":
            user.category = "student"
            promoted = True
        changed_users += 1
        click.echo(
            f"{user.username} ({user.email}): added to {', '.join(added)}"
            + (" [promoted to student]" if promoted else "")
        )
    if dry_run:
        db.session.rollback()
        click.echo(f"[dry-run] {changed_users} user(s) would be updated")
    else:
        db.session.commit()
        click.echo(f"{changed_users} user(s) updated")
