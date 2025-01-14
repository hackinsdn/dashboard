# -*- encoding: utf-8 -*-
import os, random, string
from flask import Flask
from flask_mail import Mail, Message
from dotenv import load_dotenv
from email.mime.text import MIMEText
import base64
from flask import Flask, redirect, url_for, session
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import os

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

app = Flask(__name__)

# Usando as variáveis de ambiente
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT'))
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS') == 'True'
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL') == 'True'

mail = Mail(app)

@app.route("/")
def index():
    msg = Message(
        subject='Hello from the other side!',
        sender=os.getenv('MAIL_USERNAME'),  # E-mail do remetente carregado da variável de ambiente
        recipients=[os.getenv('MAIL_USERNAME')]  # E-mail do destinatário carregado da variável de ambiente
    )
    msg.body = "Hey, sending you this email from my Flask app, let me know if it works."
    mail.send(msg)
    return "Message sent!"

if __name__ == '__main__':
    app.run(debug=True)


app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'chave_padrão_segura')

load_dotenv()

# Replace 'your_credentials.json' with the path to your downloaded credentials file
flow = Flow.from_client_secrets_file(
    '/home/beatriz/projects/hackinsdn/dashboard/credentials.json',
    scopes=['https://www.googleapis.com/auth/gmail.send'],
    redirect_uri='http://localhost:5000/callback'
)

@app.route('/')
def index():
    authorization_url, state = flow.authorization_url(prompt='consent')
    session['state'] = state
    return redirect(authorization_url)

@app.route('/callback')
def callback():
    flow.fetch_token(authorization_response=request.url)

    if not session['state'] == request.args['state']:
        abort(500)  # State does not match!

    credentials = flow.credentials
    session['credentials'] = {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }
    
    return 'Authentication successful, you can now send emails.'

if __name__ == '__main__':
    app.run(debug=True)


def send_email(service, user_id, message):
    try:
        message = (service.users().messages().send(userId=user_id, body=message)
                   .execute())
        print('Message Id: %s' % message['id'])
        return message
    except Exception as error:
        print('An error occurred: %s' % error)

def create_message(sender, to, subject, message_text):
    message = MIMEText(message_text)
    message['to'] = to
    message['from'] = sender
    message['subject'] = subject
    return {'raw': base64.urlsafe_b64encode(message.as_string().encode()).decode()}
