# -*- encoding: utf-8 -*-
"""HackInSDN."""

from flask import Blueprint

blueprint = Blueprint(
    'api_blueprint',
    __name__,
    url_prefix='/api'
)

