# -*- encoding: utf-8 -*-

import uuid
from flask_login import UserMixin
from sqlalchemy.orm import validates
import enum
from apps import db, login_manager

from sqlalchemy import UniqueConstraint
from apps.authentication.util import hash_pass
from apps.audit_mixin import AuditMixin, utcnow

def user_category():
    return "user"

def generate_uid():
    return uuid.uuid4().hex[:14]

class Users(db.Model, UserMixin, AuditMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True)
    uid = db.Column(db.String(15), unique=True, default=generate_uid)
    email = db.Column(db.String(64))
    password = db.Column(db.LargeBinary)
    category = db.Column(db.String(10), default=user_category)
    given_name = db.Column(db.String(64))
    family_name = db.Column(db.String(64))
    subject = db.Column(db.String(255))
    issuer = db.Column(db.String(255))
    active = db.Column(db.Boolean, default=True)

    groups = db.relationship("GroupMembers",back_populates="user")

    def __init__(self, **kwargs):
        for property, value in kwargs.items():
            # depending on whether value is an iterable or not, we must
            # unpack it's value (when **kwargs is request.form, some values
            # will be a 1-element list)
            if hasattr(value, '__iter__') and not isinstance(value, str):
                # the ,= unpack of a singleton fails PEP8 (travis flake8 test)
                value = value[0]

            if property == 'password':
                value = hash_pass(value)  # we need bytes here (not plain str)

            setattr(self, property, value)

        if "uid" not in kwargs:
            self.uid = generate_uid()
        if "username" not in kwargs:
            self.username = f"user-{self.uid}"

    def __repr__(self):
        return str(self.username)

    @property
    def name(self):
        full_name = " ".join(filter(None, [self.given_name or "", self.family_name or ""]))
        if not full_name:
            return self.username
        return full_name

    def set_password(self, value):
        self.password = hash_pass(value)

    @validates("category")
    def validate_category(self, key, value):
        allowed = ["admin", "teacher", "student", "user"]
        if value not in allowed:
            raise ValueError(f"Invalid user category: {value} -- {allowed}")
        return value

class MemberType(enum.Enum):
    regular = 1
    assistant = 2
    owner = 3

class GroupMembers(db.Model):
    __tablename__ = 'group_members'
    
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    member_type = db.Column(db.Integer, primary_key=True)  

    __table_args__ = (UniqueConstraint('user_id', 'group_id', name='_user_group_uc'),)

    user = db.relationship( "Users" , back_populates="groups")  
    group = db.relationship( "Groups",back_populates="users")  


@login_manager.user_loader
def user_loader(id):
    return Users.query.filter_by(id=id).first()


@login_manager.request_loader
def request_loader(request):
    username = request.form.get('username')
    user = Users.query.filter_by(username=username).first()
    return user if user else None

class Groups(db.Model):
    __tablename__ = 'groups'
    id = db.Column(db.Integer, primary_key=True)
    groupname = db.Column(db.String(64))
    uid = db.Column(db.String(15), unique=True, default=generate_uid)
    description = db.Column(db.String)
    organization = db.Column(db.String)
    expiration = db.Column(db.DateTime, nullable=True)
    approved_users = db.Column(db.Text, nullable=True)
    accesstoken = db.Column(db.String)

    users = db.relationship("GroupMembers",back_populates="group", cascade='all, delete-orphan')

    def __init__(self, **kwargs):
        for property, value in kwargs.items():
            if hasattr(value, '__iter__') and not isinstance(value, str):
                value = value[0]

            if property == 'accesstoken':
                value = hash_pass(value) 

            setattr(self, property, value)

    def __repr__(self):
        return f'<Group {self.groupname}>'


class LoginLogging(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ipaddr = db.Column(db.String)
    login = db.Column(db.String)
    auth_provider = db.Column(db.String(10))
    success = db.Column(db.Boolean)
    datetime = db.Column(db.DateTime, default=utcnow, nullable=False)

    def __repr__(self):
        return '<LoginLogging %s %s %s>' % (self.ip_address,
                                            self.login, self.datetime)