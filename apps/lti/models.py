# -*- encoding: utf-8 -*-
from __future__ import annotations
import datetime
import hashlib
import json

from sqlalchemy.orm import validates

from apps import db
from apps.audit_mixin import AuditMixin, utcnow


def normalize_issuer(issuer):
    """Issuer = LMS base URL, exact string (scheme/host/port, no trailing
    slash) - it must match the `iss` claim character-for-character, so any
    trailing slashes are stripped on write, never on read."""
    return (issuer or "").strip().rstrip("/")


class LtiConfig(db.Model, AuditMixin):
    __tablename__ = "lti_config"
    id = db.Column(db.Integer, primary_key=True)
    issuer = db.Column(db.String(255), unique=True, nullable=False, index=True)
    _config = db.Column("config", db.Text, nullable=False, default="[]")
    is_deleted = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<LtiConfig {self.issuer}>"

    @validates("issuer")
    def validate_issuer(self, key, value):
        value = normalize_issuer(value)
        if not value:
            raise ValueError("LtiConfig issuer cannot be empty")
        return value

    @property
    def config(self):
        """Per-issuer list of platform registrations (JSON-encoded in the DB):
        {client_id, auth_login_url, auth_token_url, key_set_url,
        deployment_ids, private_key_file, public_key_file, default}.
        Key file paths are relative to DATA_DIR."""
        if not self._config:
            return []
        return json.loads(self._config)

    @config.setter
    def config(self, value):
        self._config = json.dumps(value)

    def get_registration(self, client_id=None):
        """Return the registration entry for client_id, or the default
        (first) entry when client_id is not given."""
        regs = self.config
        if client_id:
            for reg in regs:
                if reg.get("client_id") == client_id:
                    return reg
            return None
        for reg in regs:
            if reg.get("default"):
                return reg
        return regs[0] if regs else None

    def upsert_registration(self, registration):
        """Insert or replace (by client_id) one registration entry.
        The first entry registered for the issuer stays the default one."""
        regs = self.config
        for i, reg in enumerate(regs):
            if reg.get("client_id") == registration.get("client_id"):
                registration["default"] = reg.get("default", False)
                regs[i] = registration
                break
        else:
            registration["default"] = len(regs) == 0
            regs.append(registration)
        self.config = regs


class LtiRegistrationToken(db.Model):
    """One-time credential for the dynamic registration endpoint. Only the
    SHA-256 hash is stored; the token itself is printed once by the
    `flask lti mint-registration-token` command. Consumed (used_at set) only
    after a successful registration so failed attempts remain retryable."""
    __tablename__ = "lti_registration_tokens"
    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    label = db.Column(db.String(255))
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    @staticmethod
    def hash_token(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @property
    def is_expired(self):
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        return expires_at < utcnow()

    @classmethod
    def find_valid(cls, token):
        row = cls.query.filter_by(token_hash=cls.hash_token(token)).first()
        if not row or row.used_at or row.is_expired:
            return None
        return row
