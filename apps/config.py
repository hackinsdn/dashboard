# -*- encoding: utf-8 -*-
import os, random, string
from flask import Flask, Blueprint, render_template
from flask_mail import Mail, Message
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from apps.authentication.models import Users
import pytz

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
    #K8S_CONFIG = os.path.expanduser("/home/italo/.kube/config")
    K8S_CONFIG = os.path.expanduser("~/.kube/config")
    K8S_AVOID_NODES = ["whx-rn", "ids-pb", "ids-pe", "vm1-ac", "vm1-mt", "whx-pb", "whx-rn"]

    # Base URL
    BASE_URL = 'https://dashboard.hackinsdn.ufba.br'

    LOG_FMT = "%(asctime)s %(levelname)s [%(name)s] [%(filename)s:%(lineno)d] %(message)s"
    LOG_FILE = os.getenv("LOG_FILE")
    
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

# Carregar variáveis de ambiente do arquivo .env

load_dotenv()

mail_blueprint = Blueprint('mail', __name__)  # Cria o blueprint

app = Flask(__name__)
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT'))
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS') == 'True'
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL') == 'True'

mail = Mail(app)

@mail_blueprint.route("/email")
def send_email():
    utc=pytz.UTC
    # Calcula o tempo limite de 1 hora atrás
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    one_hour_ago=one_hour_ago.replace(tzinfo=utc)
    # Busca todos os usuários com mais de 1h de espera para validação
    users = Users.query.filter(
            Users.category == "user",  # Filtra por categoria "user"
            Users.created_at <= one_hour_ago  # Verifica se foi criado há mais de 1 hora
        ).all()

    if not users:  # Corrigido de all_users para users
        return "No users found."

    # Lista para armazenar usuários que atendem aos critérios
    valid_users = []
    for user in users:
        # Certifica-se de que `created_at` tem fuso horário (offset-aware)
        if user.created_at.tzinfo is None:
            user.created_at = user.created_at.replace(tzinfo=utc)

        # Verifica se o usuário está ativo e atende ao critério de tempo
        if user.created_at < one_hour_ago :
            valid_users.append(user)  # Adiciona o usuário à lista de válidos

    # Envia e-mails para usuários válidos
    if valid_users:
        for user in valid_users:
            msg = Message(
                subject="Approval Pending",
                sender = os.getenv('MAIL_NAME'),
                recipients=os.getenv('RECIPIENTS'),  # Enviar para o seu próprio e-mail
                body=f"Hello,\n\n {user.name} have been waiting for approval for more than an hour."
            )
            mail.send(msg)

        return "Emails sent to valid users."
    else:
        return "No valid users found."