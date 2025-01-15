from apps import mail 
from flask_mail import Message
from apps.authentication.models import Users
import pytz
from datetime import datetime, timezone, timedelta
from apps import create_app
from apps.config import Config

def send_email():
    app = create_app(Config)  #

    with app.app_context():  # Contexto do Flask para garantir que as operações sejam feitas no app correto
        utc = pytz.UTC
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        one_hour_ago = one_hour_ago.replace(tzinfo=utc)  # Garantir que one_hour_ago tenha fuso horário UTC

        # Consulta para pegar usuários pendentes de aprovação
        users = Users.query.filter(
                Users.category == "user",
                Users.created_at <= one_hour_ago
            ).all()

        if not users:
            print("No users found.")
            return "No users found."

        # Lista para armazenar os usuários pendentes
        valid_users = []
        for user in users:
            # Verificar se user.created_at é um datetime sem fuso horário e, se for, adicionar fuso horário UTC
            if user.created_at.tzinfo is None:
                user.created_at = user.created_at.replace(tzinfo=timezone.utc)

            # Comparar datetime com fuso horário
            if user.created_at < one_hour_ago:
                valid_users.append(user)

        if valid_users:
            body = "List of users pending approval:\n\n"
            for user in valid_users:
                body += f"Name: {user.username}, Email: {user.email}"

            msg = Message(
                subject="Pending User Approvals",
                sender=app.config['MAIL_USERNAME'],  
                recipients=[app.config['MAIL_USERNAME']], 
                body=body
            )
            mail.send(msg)  
            return "Emails sent to admin."
        else:
            return "No valid users found."
if __name__ == "__main__":
    result = send_email()  # Execute a função que envia o e-mail
    print(result)  # Ex