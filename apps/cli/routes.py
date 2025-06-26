# -*- encoding: utf-8 -*-
"""HackInSDN"""

import click
from flask import current_app
from apps.cli import blueprint
from apps.cli.lab_schedule import alert_expiring_labs, run_delete_expired_labs

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
