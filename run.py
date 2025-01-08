# -*- encoding: utf-8 -*-

import os
import logging
from   flask_migrate import Migrate
#from   flask_minify  import Minify
from   sys import exit

from apps.config import app_config
from apps import create_app, db

# WARNING: Don't run with debug turned on in production!
DEBUG = (os.getenv('DEBUG', 'False') == 'True')

app = create_app(app_config)
Migrate(app, db)

#if not DEBUG:
#    Minify(app=app, html=True, js=False, cssless=False)

#handler = logging.FileHandler('dashboard-hackinsdn.log')
#handler.setLevel(logging.DEBUG)
#app.logger.addHandler(handler)
#log = logging.getLogger('werkzeug')
#log.setLevel(logging.DEBUG)
#log.addHandler(handler)

    
if DEBUG:
    app.logger.info('DEBUG            = ' + str(DEBUG)             )
    app.logger.info('Page Compression = ' + 'FALSE' if DEBUG else 'TRUE' )
    app.logger.info('DBMS             = ' + app_config.SQLALCHEMY_DATABASE_URI)
    app.logger.info('ASSETS_ROOT      = ' + app_config.ASSETS_ROOT )

if __name__ == "__main__":
    app.run()
