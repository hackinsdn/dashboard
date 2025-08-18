# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - present AppSeed.us
"""

import apps.events  # noqa: F401
from flask import Blueprint

blueprint = Blueprint(
    'home_blueprint',
    __name__,
    url_prefix=''
)

