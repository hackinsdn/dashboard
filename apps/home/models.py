# -*- encoding: utf-8 -*-

from apps import db
from apps.authentication.models import Users
from apps.audit_mixin import AuditMixin, get_user_id, utcnow
import uuid
import secrets
import json
import markdown2
from sqlalchemy import event
from flask import current_app as app


def generate_uuid():
    return str(uuid.uuid4())

def generate_uuid_14():
    return uuid.uuid4().hex[:14]

def generate_token():
    return secrets.token_urlsafe(64)


lab_groups = db.Table('lab_groups',
    db.Column('lab_id', db.String(40), db.ForeignKey('labs.id'), primary_key=True),
    db.Column('group_id', db.Integer, db.ForeignKey('groups.id'), primary_key=True)
)


class LabCategories(db.Model):
    __tablename__ = 'lab_categories'
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(64))
    color_cls = db.Column(db.String(16))

class Labs(db.Model, AuditMixin):
    __tablename__ = 'labs'
    id = db.Column(db.String(40), primary_key=True, default=generate_uuid)
    title = db.Column(db.String(255))
    description = db.Column(db.String)
    category_id = db.Column(db.Integer, db.ForeignKey("lab_categories.id"), nullable=False)
    extended_desc = db.Column(db.LargeBinary)
    lab_guide_md = db.Column(db.LargeBinary)
    lab_guide_html = db.Column(db.LargeBinary)
    goals = db.Column(db.Text)
    manifest = db.Column(db.Text)
    allowed_groups = db.relationship('Groups', secondary=lab_groups, lazy='subquery',
                                     backref=db.backref('labs', lazy=True))

    def set_extended_desc(self, desc_str):
        self.extended_desc = desc_str.encode()

    @property
    def extended_desc_str(self):
        return self.extended_desc.decode()

    def set_lab_guide_md(self, lab_guide_md):
        self.lab_guide_md = lab_guide_md.encode()
        self.lab_guide_html = markdown2.markdown(lab_guide_md, extras=['cuddled-lists', 'fenced-code-blocks', 'alerts']).encode()

    @property
    def lab_guide_html_str(self):
        return self.lab_guide_html.decode()

    @property
    def lab_guide_md_str(self):
        return self.lab_guide_md.decode()

class LabInstances(db.Model, AuditMixin):
    __tablename__ = 'lab_instances'
    id = db.Column(db.String(15), primary_key=True, default=generate_uuid_14)
    token = db.Column(db.String, default=generate_token)
    _k8s_resources = db.Column(db.String)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    lab_id = db.Column(db.String(36), db.ForeignKey("labs.id"))
    is_deleted = db.Column(db.Boolean, default=False)

    def __init__(self, id, user, lab, k8s_resources):
        self.id = id
        self.user_id = user.id
        self.lab_id = lab.id
        self.k8s_resources = k8s_resources

    @property
    def k8s_resources(self):
        return json.loads(self._k8s_resources)

    @k8s_resources.setter
    def k8s_resources(self, k8s_resources):
        self._k8s_resources = json.dumps(k8s_resources)

    def get_user(self):
        user = Users.query.get(self.user_id)
        return user.realname

    def get_lab(self):
        lab = Labs.query.get(self.lab_id)
        return lab.title


class LabAnswers(db.Model, AuditMixin):
    __tablename__ = 'lab_answers'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    lab_id = db.Column(db.String(36), db.ForeignKey("labs.id"))
    answers = db.Column(db.String)

    @property
    def answers_dict(self):
        try:
            return json.loads(self.answers)
        except:
            return {}

    @property
    def answers_table(self):
        data = self.answers_dict
        output = "<table class='table table-bordered'>"
        output += "<tr><th>Question</th><th>Answer</th></tr>"
        for k, v in data.items():
            output += f"<tr><td><b>{k}</b></td><td>{v}</td></tr>"
        output += "</table>"
        return output


class LabAnswerSheet(db.Model, AuditMixin):
    __tablename__ = 'lab_answer_sheet'
    id = db.Column(db.Integer, primary_key=True)
    lab_id = db.Column(db.String(36), db.ForeignKey("labs.id"))
    answers = db.Column(db.String)

    @property
    def answers_dict(self):
        try:
            return json.loads(self.answers)
        except:
            return {}

    def set_answers(self, data):
        self.answers = json.dumps(data)

class HomeLogging(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ipaddr = db.Column(db.String)
    action = db.Column(db.String)
    success = db.Column(db.Boolean)
    datetime = db.Column(db.DateTime, default=utcnow, nullable=False)
    lab_id = db.Column(db.String(36), db.ForeignKey("labs.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    def __repr__(self):
        return '<HomeLogging %s %s %s>' % (self.ip_address,
                                            self.action, self.datetime)

class UserLikes(db.Model):
    __tablename__ = 'user_likes'
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key = True, nullable=False)

    def __repr__(self):
        return f'<UserLikes User {self.user_id}>'


class UserFeedbacks(db.Model):
    __tablename__ = 'user_feedbacks'
    id = db.Column(db.Integer, primary_key = True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    comment = db.Column(db.String, nullable=True)
    stars = db.Column(db.Integer, nullable=False)
    hide = db.Column(db.Boolean, default=False, nullable=False)

    def __repr__(self):
        return f'<UserFeedbacks User {self.user_id}, Stars {self.stars}>'


@event.listens_for(Labs, 'after_insert')
@event.listens_for(LabInstances, 'after_insert')
def logging_added(mapper, connection, target):
    app.logger.info(f"object added target={target} user={get_user_id()}")

@event.listens_for(Labs, 'after_update')
@event.listens_for(LabInstances, 'after_update')
def logging_changed(mapper, connection, target):
    app.logger.info(f"object changed target={target} user={get_user_id()}")

@event.listens_for(LabInstances, 'after_delete')
@event.listens_for(Labs, 'after_delete')
def logging_deleted(mapper, connection, target):
    app.logger.info(f"object deleted target={target} user={get_user_id()}")
