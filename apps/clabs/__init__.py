# -*- encoding: utf-8 -*-
from flask import Blueprint

blueprint = Blueprint(
    "clabs_blueprint",
    __name__,
    url_prefix="/containerlabs",
    template_folder="templates",
    static_folder="static",
)
