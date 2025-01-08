# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - present AppSeed.us
"""

import os

from flask import Flask
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from importlib import import_module
from authlib.integrations.flask_client import OAuth


db = SQLAlchemy()
login_manager = LoginManager()
oauth = OAuth()
socketio = SocketIO(cors_allowed_origins="*")
log = None


def register_extensions(app):
    db.init_app(app)
    login_manager.init_app(app)
    oauth.init_app(app)
    socketio.init_app(app)


def register_blueprints(app):
    for module_name in ('authentication', 'home', 'api'):
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

def create_app(config):
    global log
    app = Flask(__name__)
    app.config.from_object(config)
    register_extensions(app)
    register_blueprints(app)
    configure_database(app)
    configure_oauth(app)
    log = app.logger
    return app
