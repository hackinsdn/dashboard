# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - present AppSeed.us
"""

# Evita erro no Windows (pty/termios) quando rodando localmente
try:
    import apps.events  # noqa: F401
except Exception as e:
    print("⚠ Skipping apps.events in local Windows dev:", e)

from flask import Blueprint

blueprint = Blueprint(
    'home_blueprint',
    __name__,
    url_prefix=''
)

