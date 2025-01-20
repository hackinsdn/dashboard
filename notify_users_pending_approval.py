from apps import mail 
from flask_mail import Message
from apps.authentication.models import Users
import pytz
from datetime import datetime, timezone, timedelta
from apps import create_app
from apps.config import Config
from apps.audit_mixin import utcnow

def send_email():
    app = create_app(Config)
    if not app.config["MAIL_SENDTO"]:
        return


    with app.app_context():

        # Consulta para pegar usuários pendentes de aprovação
        users = Users.query.filter(
                Users.category == "user",
                Users.created_at <= one_hour_ago,
                Users.active==True
            ).all()

        if not users:
            return "No users found."

        body = "List of users pending approval:\n\n"
        for user in users:
            body += f"Name: {user.username}, Email: {user.email}, Created-At: {user.created_at}\n"

        msg = Message(
            subject="[Dashboard HackInSDN] Pending User Approvals",
            sender=app.config['MAIL_USERNAME'],  
            recipients=[app.config['MAIL_SENDTO']], 
            body=body
        )
        mail.send(msg)  
            
if __name__ == "__main__":
    send_email() 
