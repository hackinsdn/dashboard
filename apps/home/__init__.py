# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - 2024 AppSeed.us
Copyright (c) 2014 - present HackInSDN team
"""

import apps.events # noqa: F401
from flask import Blueprint

blueprint = Blueprint(
    'home_blueprint',
    __name__,
    url_prefix=''
)

