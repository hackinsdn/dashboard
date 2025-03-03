# -*- encoding: utf-8 -*-

from flask_migrate import Migrate
from apps.config import app_config
from apps import create_app, db

app = create_app(app_config)
Migrate(app, db)

if __name__ == "__main__":
    app.run(debug=app_config.DEBUG)
