# -*- encoding: utf-8 -*-
"""HackInSDN."""

from flask import Blueprint

blueprint = Blueprint(
    'k8s_blueprint',
    __name__,
    url_prefix='/k8s'
)

