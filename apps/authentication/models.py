# -*- encoding: utf-8 -*-
from __future__ import annotations
import uuid
import json
from typing import List
from flask_login import UserMixin
from sqlalchemy import event, insert
from sqlalchemy.orm import validates
from sqlalchemy.orm import Mapped
from apps import db, login_manager

from apps.authentication.util import hash_pass
from apps.audit_mixin import AuditMixin, utcnow


def user_category():
    return "user"

def generate_uid():
    return uuid.uuid4().hex[:14]


group_members = db.Table(
    "group_members",
    db.Model.metadata,
    db.Column("group_id", db.ForeignKey("groups.id"), primary_key=True),
    db.Column("user_id", db.ForeignKey("users.id"), primary_key=True),
)


group_owners = db.Table(
    "group_owners",
    db.Model.metadata,
    db.Column("group_id", db.ForeignKey("groups.id"), primary_key=True),
    db.Column("user_id", db.ForeignKey("users.id"), primary_key=True),
)

group_assistants = db.Table(
    "group_assistants",
    db.Model.metadata,
    db.Column("group_id", db.ForeignKey("groups.id"), primary_key=True),
    db.Column("user_id", db.ForeignKey("users.id"), primary_key=True),
)


class Users(db.Model, UserMixin, AuditMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True)
    uid = db.Column(db.String(15), unique=True)
    email = db.Column(db.String(64))
    password = db.Column(db.LargeBinary)
    category = db.Column(db.String(10), default=user_category)
    given_name = db.Column(db.String(64))
    family_name = db.Column(db.String(64))
    subject = db.Column(db.String(255))
    issuer = db.Column(db.String(255))
    is_deleted = db.Column(db.Boolean, default=False)

    member_of_groups: Mapped[List[Groups]] = db.relationship(
        secondary=group_members, back_populates="members"
    )
    assistant_of_groups: Mapped[List[Groups]] = db.relationship(
        secondary=group_assistants, back_populates="assistants"
    )
    owner_of_groups: Mapped[List[Groups]] = db.relationship(
        secondary=group_owners, back_populates="owners"
    )

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
        allowed = ["admin", "teacher", "labcreator", "student", "user"]
        if value not in allowed:
            raise ValueError(f"Invalid user category: {value} -- {allowed}")
        return value

    @property
    def privileged_group_ids(self):
        """Returns a set of group IDs for owner and assistant membership type."""
        group_ids = set()
        for group in self.assistant_of_groups:
            group_ids.add(group.id)
        for group in self.owner_of_groups:
            group_ids.add(group.id)
        return group_ids

    @property
    def all_group_ids(self):
        """Returns a set of group IDs for all types of membership."""
        group_ids = self.privileged_group_ids
        for group in self.member_of_groups:
            group_ids.add(group.id)
        return group_ids


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
    is_deleted = db.Column(db.Boolean, default=False)
    groupname = db.Column(db.String(64))
    uid = db.Column(db.String(15), unique=True, default=generate_uid)
    description = db.Column(db.String)
    organization = db.Column(db.String)
    expiration = db.Column(db.DateTime, nullable=True)
    approved_users = db.Column(db.Text, nullable=True)
    accesstoken = db.Column(db.String(32))

    members: Mapped[List[Users]] = db.relationship(
        secondary=group_members, back_populates="member_of_groups"
    )
    assistants: Mapped[List[Users]] = db.relationship(
        secondary=group_assistants, back_populates="assistant_of_groups"
    )
    owners: Mapped[List[Users]] = db.relationship(
        secondary=group_owners, back_populates="owner_of_groups"
    )

    def __repr__(self):
        return f'<Group {self.groupname}>'

    @property
    def approved_users_list(self):
        if not self.approved_users:
            return []
        return json.loads(self.approved_users)

    @property
    def approved_users_str(self):
        if not self.approved_users:
            return ""
        try:
            data = json.loads(self.approved_users)
        except:
            return self.approved_users
        return "\n".join(data)

    def set_approved_users(self, approved_users):
        self.approved_users = json.dumps(approved_users)

    @property
    def members_dict(self):
        return {user.id: user for user in self.members}

    @property
    def owners_dict(self):
        return {user.id: user for user in self.owners}

    @property
    def assistants_dict(self):
        return {user.id: user for user in self.assistants}

    def is_owner(self, user_id):
        return db.session.query(group_owners).filter(
            group_owners.c.group_id==self.id, group_owners.c.user_id==user_id
        ).count() == 1

    def is_member(self, user_id):
        return db.session.query(group_members).filter(
            group_members.c.group_id==self.id, group_members.c.user_id==user_id
        ).count() == 1

    def is_assistant(self, user_id):
        return db.session.query(group_assistants).filter(
            group_assistants.c.group_id==self.id, group_assistants.c.user_id==user_id
        ).count() == 1


class DeletedGroupUsers(db.Model):
    __tablename__ = 'deleted_group_users'
    id = db.Column(db.Integer, primary_key=True)
    object_id = db.Column(db.Integer)
    object_type = db.Column(db.String(6))
    members = db.Column(db.String)
    owners = db.Column(db.String)
    assistants = db.Column(db.String)
    datetime = db.Column(db.DateTime, default=utcnow, nullable=False)


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


@event.listens_for(Users, 'after_insert')
def add_user_to_sysgrp_everybody(mapper, connection, user):
    everybody = Groups.query.filter_by(groupname="Everybody", organization="SYSTEM").first()
    if not everybody:
        return
    connection.execute(insert(group_members), dict(user_id=user.id, group_id=everybody.id))
