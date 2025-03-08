# -*- encoding: utf-8 -*-
import os, random, string
from dotenv import load_dotenv


load_dotenv()


class Config(object):

    basedir = os.path.abspath(os.path.dirname(__file__))

    # Directories
    DATA_DIR = os.getenv('DATA_DIR', os.path.join(basedir, 'data'))
    UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # Assets Management
    ASSETS_ROOT = os.getenv('ASSETS_ROOT', '/static/assets')

    # Set up the App SECRET_KEY
    SECRET_KEY  = os.getenv('SECRET_KEY', "")
    if not SECRET_KEY:
        SECRET_KEY = ''.join(random.choice( string.ascii_lowercase  ) for i in range( 32 ))    

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    DB_ENGINE   = os.getenv('DB_ENGINE'   , None)
    DB_USERNAME = os.getenv('DB_USERNAME' , None)
    DB_PASS     = os.getenv('DB_PASS'     , None)
    DB_HOST     = os.getenv('DB_HOST'     , None)
    DB_PORT     = os.getenv('DB_PORT'     , None)
    DB_NAME     = os.getenv('DB_NAME'     , None)

    USE_SQLITE  = True 

    # try to set up a Relational DBMS
    if DB_ENGINE and DB_NAME and DB_USERNAME:

        try:
            
            # Relational DBMS: PSQL, MySql
            SQLALCHEMY_DATABASE_URI = '{}://{}:{}@{}:{}/{}'.format(
                DB_ENGINE,
                DB_USERNAME,
                DB_PASS,
                DB_HOST,
                DB_PORT,
                DB_NAME
            ) 

            USE_SQLITE  = False

        except Exception as e:

            print('> Error: DBMS Exception: ' + str(e) )
            print('> Fallback to SQLite ')    

    if USE_SQLITE:

        # This will create a file in <app> FOLDER
        SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(DATA_DIR, 'db.sqlite3') 

    # OAUTH Authentication
    CLIENT_ID     = os.getenv('OAUTH_CLIENT_ID', "")
    CLIENT_SECRET = os.getenv('OAUTH_CLIENT_SECRET', "")
    DOMAIN        = os.getenv('OAUTH_DOMAIN', "")

    # Kubernetes
    K8S_NAMESPACE = os.getenv('K8S_NAMESPACE', "")
    K8S_CONFIG = os.path.expanduser(os.getenv("KUBECONFIG", "~/.kube/config"))
    K8S_AVOID_NODES = ["whx-rn", "ids-pb", "ids-pe", "vm1-ac", "vm1-mt", "whx-pb", "whx-rn"]

    # Base URL
    BASE_URL = os.getenv("BASE_URL", 'https://dashboard.hackinsdn.ufba.br')

    LOG_FMT = "%(asctime)s %(levelname)s [%(name)s] [%(filename)s:%(lineno)d] %(message)s"
    LOG_FILE = os.getenv("LOG_FILE")

    MAIL_SERVER = os.getenv("MAIL_SERVER")
    MAIL_PORT = os.getenv("MAIL_PORT", 587)
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER")
    MAIL_USE_TLS = True
    MAIL_USE_SSL = False
    MAIL_SENDTO = os.getenv("MAIL_SENDTO")

    # Flask Cache (https://flask-caching.readthedocs.io/en/latest/)
    CACHE_TYPE = "SimpleCache"
    CACHE_DEFAULT_TIMEOUT = int(os.getenv("CACHE_DEFAULT_TIMEOUT", 300))

class ProductionConfig(Config):
    DEBUG = False

    # Security
    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_DURATION = 3600


class DevelopmentConfig(Config):
    DEBUG = True


# Load all possible configurations
config_dict = {
    'Production': ProductionConfig,
    'Development'     : DevelopmentConfig
}

# WARNING: Don't run with debug turned on in production!
DEBUG = (os.getenv('DEBUG', 'False') == 'True')

# The configuration
get_config_mode = 'Development' if DEBUG else 'Production'

try:

    # Load the configuration using the default values
    app_config = config_dict[get_config_mode.capitalize()]()

except KeyError:
    raise ValueError('Error: Invalid <config_mode>. Expected values [Debug, Production] ')
