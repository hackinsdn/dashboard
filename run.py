# -*- encoding: utf-8 -*-

from flask_migrate import Migrate
from flask_mail import Mail
from apps.config import app_config
from apps import create_app, db

app = create_app(app_config)
Migrate(app, db)
mail = Mail(app) # Initializing Flask-Mail with the app

if __name__ == "__main__":
    app.run()
