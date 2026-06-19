# -*- encoding: utf-8 -*-

from apps.config import app_config
from apps import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=app_config.DEBUG)
