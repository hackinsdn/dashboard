import datetime
from sqlalchemy import Column, DateTime, Integer, ForeignKey
from flask_login import current_user
from flask import request

def utcnow():
    return datetime.datetime.now(datetime.UTC)


def get_user_id():
    try:
        return current_user.id
    except:
        return None


def get_remote_addr():
    try:
        return request.remote_addr
    except:
        return "LOCAL"


class AuditMixin(object):
    created_at = Column(DateTime, default=utcnow())
    updated_at = Column(DateTime, default=utcnow(), onupdate=utcnow())
    updated_by = Column(Integer, ForeignKey("users.id"), default=get_user_id)
