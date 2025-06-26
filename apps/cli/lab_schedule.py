from flask_mail import Mail, Message
from datetime import datetime, timedelta, timezone
from apps.home.models import LabInstances, Labs
from apps.authentication.models import Users
from apps.audit_mixin import utcnow
from flask import render_template
from apps import db 
from apps.config import app_config
from apps.controllers import k8s
from apps.utils import datetime_from_ts


def alert_expiring_labs(app, send_email=False):
    target_ts = int(utcnow().timestamp() + app_config.LAB_EXPIRATION_WARN_SEC)
    lab_instances = LabInstances.query.filter(
        LabInstances.is_deleted == False,
        LabInstances.expiration_ts.isnot(None),
        LabInstances.expiration_ts <= target_ts,
    ).all()
    app.logger.info(f"Checking for expiring labs. Found {len(lab_instances)} lab instances expiring..")
    for lab_instance in lab_instances:
        lab = Labs.query.get(lab_instance.lab_id)
        end_time = datetime_from_ts(lab_instance.expiration_ts)
        user = Users.query.get(lab_instance.user_id)
        if not user or not send_email:
            app.logger.info(f"ALERT Expiring Lab {user=} {lab_instance.id=} {end_time=}. No e-mail to send..")
            continue
        if user.email:
            app.logger.info(
                f"ALERT Expiring Lab {user=} {lab_instance.id=} {end_time=}. "
                f"Sending alert to {user.email}"
            )
            mail = Mail(app)
            msg = Message(
                subject="HackInSDN - Your lab is about to expire",
                sender=app.config["MAIL_DEFAULT_SENDER"],
                recipients=[user.email],
                body=f"Hello {user.username},\n\n"
                        f"Your lab '{lab.title}' is scheduled to expire on {end_time}.\n\n"
                        "You can extend your lab execution and continue to use it! Visit the web interface to update your lab.\n\n"
                        "Please make sure to save any important data before the expiration time.\n\n"
                        "Best regards,\n"
                        "HackInSDN Team",
                html= render_template(
                    "mail/lab_expiration_alert.html",
                    user=user,
                    lab=lab,
                    end_time=end_time
                )
            )
            mail.send(msg)
    app.logger.info("Checking for expiring labs: done")


def run_delete_expired_labs(app):
    target_ts = int(utcnow().timestamp() - app_config.LAB_EXPIRATION_TOLERANACE_SEC)
    lab_instances = LabInstances.query.filter(
        LabInstances.is_deleted == False,
        LabInstances.expiration_ts.isnot(None),
        LabInstances.expiration_ts <= target_ts,
    ).all()
    app.logger.info(f"Removing expired labs. Found {len(lab_instances)} expired labs...")
    for lab_instance in lab_instances:
        app.logger.info(f"ALERT Lab expired {lab_instance.user_id=} {lab_instance.id=}: removing...")
        try:
            k8s.delete_resources_by_name(lab_instance.k8s_resources)
            lab_instance.is_deleted = True
            lab_instance.finish_reason = "lab expired"
            db.session.commit()
        except Exception as exc:
            app.logger.error(f"Error deleting resources for lab {lab_instance.id}: {exc}")
    app.logger.info("Removing expired labs: done")
