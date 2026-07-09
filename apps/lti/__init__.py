# -*- encoding: utf-8 -*-
"""HackInSDN - LTI 1.3 tool integration (optional module "lti")."""

from flask import Blueprint

blueprint = Blueprint(
    'lti_blueprint',
    __name__,
    url_prefix='/lti',
    cli_group='lti'
)
