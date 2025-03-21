import datetime
from sqlalchemy import Column, DateTime, Integer, ForeignKey
from flask_login import current_user
from flask import request
from functools import wraps
from flask import render_template

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def get_user_id():
    try:
        return current_user.id
    except:
        return None


def get_remote_addr():
    try:
        return ",".join(request.access_route)
    except:
        return "LOCAL"


class AuditMixin(object):
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by = Column(Integer, ForeignKey("users.id"), default=get_user_id)

def check_user_category(allowed_categories):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if current_user.category not in allowed_categories:
                if current_user.category == "user":
                    return render_template('pages/waiting_approval.html')
                
                return render_template("pages/error.html", title="Unauthorized request", msg="You dont have permission to see this page")
            return f(*args, **kwargs)
        return decorated_function
    return decorator