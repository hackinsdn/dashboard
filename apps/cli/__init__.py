# -*- encoding: utf-8 -*-
"""HackInSDN."""

from flask import Blueprint

blueprint = Blueprint(
    'cli_blueprint',
    __name__,
    cli_group='cli'
)

