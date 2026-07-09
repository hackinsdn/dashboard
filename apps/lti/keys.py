# -*- encoding: utf-8 -*-
"""Key management for the LTI 1.3 module.

Every platform registration gets its own RSA-2048 keypair, and rollover is
retirement-based: rotated keys move into a retired/ directory whose public
halves stay published in /lti/jwks/ until purged, so platforms validating
cached tokens keep finding the old kid (publish-then-switch rollover).
Key file paths are stored in lti_config relative to DATA_DIR.
"""
import hashlib
import json
import os
import re
import secrets
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import current_app
from jwcrypto import jwk

KEYS_DIR_REL = os.path.join("lti", "keys")
RETIRED_DIR_REL = os.path.join(KEYS_DIR_REL, "retired")
JWKS_CACHE_KEY = "lti_jwks"
JWKS_CACHE_TIMEOUT = 300


def abs_key_path(rel_path):
    return os.path.join(current_app.config["DATA_DIR"], rel_path)


def keys_dir():
    return abs_key_path(KEYS_DIR_REL)


def retired_dir():
    return abs_key_path(RETIRED_DIR_REL)


def generate_keypair(issuer, client_id):
    """Generate a fresh RSA-2048 keypair for one platform registration and
    return (private_rel_path, public_rel_path). The random suffix keeps
    rotations of the same registration from colliding."""
    os.makedirs(keys_dir(), exist_ok=True)
    iss_hash = hashlib.sha256(issuer.encode()).hexdigest()[:12]
    client_slug = re.sub(r"[^A-Za-z0-9_-]", "", client_id or "")[:32] or "client"
    basename = f"{iss_hash}_{client_slug}_{secrets.token_hex(4)}"

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    private_rel = os.path.join(KEYS_DIR_REL, basename + ".key")
    public_rel = os.path.join(KEYS_DIR_REL, basename + ".pub")
    private_abs = abs_key_path(private_rel)
    with open(private_abs, "wb") as f:
        f.write(private_pem)
    os.chmod(private_abs, 0o600)
    with open(abs_key_path(public_rel), "wb") as f:
        f.write(public_pem)
    return private_rel, public_rel


def delete_keypair(*rel_paths):
    """Remove key files (cleanup when a registration fails after keygen)."""
    for rel_path in rel_paths:
        if not rel_path:
            continue
        try:
            os.remove(abs_key_path(rel_path))
        except OSError:
            pass


def retire_keypair(private_rel, public_rel):
    """Move both key files into retired/. The public half stays published in
    the JWKS until purge_retired_keys() removes it after the grace period."""
    os.makedirs(retired_dir(), exist_ok=True)
    for rel_path in (private_rel, public_rel):
        if not rel_path:
            continue
        src = abs_key_path(rel_path)
        if not os.path.exists(src):
            continue
        os.replace(src, os.path.join(retired_dir(), os.path.basename(rel_path)))


def purge_retired_keys(older_than_days):
    """Delete retired key files older than N days. Returns removed names."""
    removed = []
    if not os.path.isdir(retired_dir()):
        return removed
    cutoff = time.time() - older_than_days * 86400
    for fname in sorted(os.listdir(retired_dir())):
        path = os.path.join(retired_dir(), fname)
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)
            removed.append(fname)
    return removed


def _jwk_from_pem(pem_bytes):
    key = jwk.JWK.from_pem(pem_bytes)
    data = json.loads(key.export_public())
    data["kid"] = key.thumbprint()
    data["alg"] = "RS256"
    data["use"] = "sig"
    return data


def build_jwks():
    """Assemble the public keyset: every active public key referenced by
    lti_config rows plus every retired public key still in its grace
    period."""
    from apps.lti.models import LtiConfig

    keys = []
    seen_kids = set()

    def add_pub(path):
        try:
            with open(path, "rb") as f:
                key_data = _jwk_from_pem(f.read())
        except Exception as exc:
            current_app.logger.warning(f"LTI jwks: skipping unreadable public key {path}: {exc}")
            return
        if key_data["kid"] not in seen_kids:
            seen_kids.add(key_data["kid"])
            keys.append(key_data)

    for row in LtiConfig.query.filter_by(is_deleted=False).all():
        for reg in row.config:
            if reg.get("public_key_file"):
                add_pub(abs_key_path(reg["public_key_file"]))

    if os.path.isdir(retired_dir()):
        for fname in sorted(os.listdir(retired_dir())):
            if fname.endswith(".pub"):
                add_pub(os.path.join(retired_dir(), fname))

    return keys
