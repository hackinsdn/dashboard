from apscheduler.schedulers.background import BackgroundScheduler
from flask_mail import Mail, Message
from datetime import datetime, timedelta
from apps.home.models import LabInstances, Labs
from apps.authentication.models import Users
from apps.audit_mixin import utcnow
from flask import render_template
from apps import db 
from apps.controllers import k8s

def parse_end_time(scheduling):
    try:
        _, end_str = scheduling.split(' - ')
        return datetime.strptime(end_str.strip(), '%d/%m/%Y %H:%M')
    except Exception:
        return None


def alert_labs(app):
    with app.app_context():
        now = utcnow().replace(tzinfo=None)
        target = now + timedelta(hours=48)
        lab_instances = LabInstances.query.filter(LabInstances.is_deleted == False).all()
        for lab_instance in lab_instances:
            lab = Labs.query.get(lab_instance.lab_id)
            end_time = parse_end_time(lab_instance.scheduling)
            if end_time:
                if abs((end_time - target).total_seconds()) < 172800:
                    user = Users.query.get(lab_instance.user_id)
                    if user and user.email:
                        print(f"Sending alert to {user.email} for lab {lab_instance.id}")
                        mail = Mail(app)
                        msg = Message(
                            subject="HackInSDN - Your Lab is About to Expire",
                            sender=app.config["MAIL_DEFAULT_SENDER"],
                            recipients=[user.email],
                            body=f"Hello {user.username},\n\n"
                                    f"Your lab '{lab.title}' is scheduled to expire on {end_time.strftime('%d/%m/%Y %H:%M')}.\n"
                                    "Please make sure to save any important data before the expiration time.\n\n"
                                    "Best regards,\n"
                                    "HackInSDN Team",
                            html= render_template(
                                "mail/lab_expiration_alert.html", 
                                user=user, 
                                lab=lab, 
                                end_time=end_time)
                        )
                        mail.send(msg)

def delete_expired_labs(app):
    with app.app_context():
        now = utcnow().replace(tzinfo=None)
        lab_instances = LabInstances.query.filter(LabInstances.is_deleted == False).all()
        for lab_instance in lab_instances:
            end_time = parse_end_time(lab_instance.scheduling)
            if end_time and end_time < now:
                try:
                    k8s.delete_resources_by_name(lab_instance.k8s_resources)
                    lab_instance.is_deleted = True
                    db.session.commit()
                except Exception as exc:
                    print(f"Error deleting resources for lab {lab_instance.id}: {exc}")
                    return


def start_scheduler(app):
    scheduler = BackgroundScheduler()
    try:
        scheduler.add_job(lambda: alert_labs(app), trigger="cron", hour=0, minute=0) # Run daily at midnight
        scheduler.add_job(lambda: delete_expired_labs(app), trigger="cron", hour=1, minute=0) # Run daily at 00:30
    except Exception as e:
        print(f"Error starting scheduler: {e}")
        return

    scheduler.start()