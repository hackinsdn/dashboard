# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - 2024 AppSeed.us
Copyright (c) 2014 - present HackInSDN Team
"""

import os
import sys
import logging

from flask import Flask
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_socketio import SocketIO
from importlib import import_module
from authlib.integrations.flask_client import OAuth
from flask_mail import Mail
from flask_caching import Cache


db = SQLAlchemy()
login_manager = LoginManager()
oauth = OAuth()
socketio = SocketIO(cors_allowed_origins="*")
mail = Mail()
cache = Cache()
migrate = Migrate()


def register_extensions(app):
    db.init_app(app)
    login_manager.init_app(app)
    oauth.init_app(app)
    socketio.init_app(app)
    mail.init_app(app)
    cache.init_app(app)
    migrate.init_app(app, db)


def register_blueprints(app):
    for module_name in ('authentication', 'home', 'api', 'k8s'):
        module = import_module('apps.{}.routes'.format(module_name))
        app.register_blueprint(module.blueprint)


def configure_database(app):
    with app.app_context():
        db.create_all()

def configure_oauth(app):
    oauth.register(
        "provider",
        client_id=app.config["CLIENT_ID"],
        client_secret=app.config["CLIENT_SECRET"],
        client_kwargs={
            "scope": "openid profile email",
        },
        server_metadata_url=f"https://{app.config['DOMAIN']}/.well-known/openid-configuration"
    )
    #app.config["AUTH_PROVIDER"] = oauth

def configure_log(app):
    level = logging.DEBUG if app.config["DEBUG"] else logging.INFO
    app.logger.setLevel(level)
    if app.config["LOG_FILE"]:
        handler = logging.FileHandler(app.config["LOG_FILE"])
        handler.setLevel(level)
        app.logger.addHandler(handler)
        # add this filehandler for webserver
        wlog = logging.getLogger('werkzeug')
        wlog.setLevel(level)
        wlog.addHandler(handler)
    for handler in app.logger.handlers:
        handler.setFormatter(logging.Formatter(fmt=app.config["LOG_FMT"]))

def create_app(config):
    app = Flask(__name__)
    app.config.from_object(config)
    register_extensions(app)
    register_blueprints(app)
    #configure_database(app)
    configure_oauth(app)
    configure_log(app)

    from scripts import lab_schedule
    lab_schedule.start_scheduler(app)

    return app
