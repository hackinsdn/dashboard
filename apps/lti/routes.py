# -*- encoding: utf-8 -*-
"""LTI 1.3 endpoints: OIDC login, message launch, public JWKS and dynamic
registration, plus the `flask lti ...` CLI commands."""

import hmac
import secrets
from datetime import timedelta
from urllib.parse import urlparse, urlsplit

import click
import requests
from flask import current_app as app
from flask import jsonify, redirect, request, session, url_for
from flask_login import login_user
from pylti1p3.contrib.flask import (
    FlaskCacheDataStorage,
    FlaskMessageLaunch,
    FlaskOIDCLogin,
    FlaskRequest,
)
from apps import cache, db
from apps.audit_mixin import get_remote_addr, utcnow
from apps.authentication.models import LoginLogging, Users
from apps.config import app_config
from apps.lti import blueprint
from apps.lti.keys import (
    JWKS_CACHE_KEY,
    JWKS_CACHE_TIMEOUT,
    build_jwks,
    delete_keypair,
    generate_keypair,
    purge_retired_keys,
    retire_keypair,
)
from apps.lti.models import (
    LtiConfig,
    LtiLaunchContext,
    LtiRegistrationToken,
    normalize_issuer,
)
from apps.lti.tool_conf import DbToolConf
from apps.utils import check_pre_approved

LTI_ROLES_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/roles"
LTI_CUSTOM_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/custom"
LTI_TOOL_CONF_CLAIM = "https://purl.imsglobal.org/spec/lti-tool-configuration"
LTI_AGS_CLAIM = "https://purl.imsglobal.org/spec/lti-ags/claim/endpoint"
LTI_DEPLOYMENT_ID_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/deployment_id"
LTI_RESOURCE_LINK_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/resource_link"
# requested at dynamic registration so the platform grants grade passback
LTI_AGS_SCOPES = (
    "https://purl.imsglobal.org/spec/lti-ags/scope/score "
    "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly"
)

REGISTRATION_CLOSE_PAGE = """<!doctype html>
<html><body>
<p>Registration completed. You can close this window and activate the tool
in the LMS.</p>
<script>
(window.opener || window.parent).postMessage(
    {subject: 'org.imsglobal.lti.close'}, '*');
</script>
</body></html>"""


@blueprint.record_once
def _warn_simplecache(state):
    # OIDC state/nonce/launch data are stored in the Flask cache between
    # /login/ and /launch/, and those two requests may hit different gunicorn
    # workers - a per-process cache breaks the flow silently.
    if state.app.config.get("CACHE_TYPE", "SimpleCache") == "SimpleCache":
        state.app.logger.warning(
            "LTI module loaded with CACHE_TYPE=SimpleCache: launches will fail "
            "when gunicorn runs multiple workers. Set CACHE_TYPE=RedisCache and "
            "CACHE_REDIS_URL for production."
        )


class _TimeoutSession(requests.Session):
    """requests.Session with a default timeout: pylti1p3's ServiceConnector
    never sets one, and grade passback runs inline in a page request - a
    hung platform must not hang the Dashboard."""
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 15)
        return super().request(method, url, **kwargs)


def get_requests_session():
    """One requests.Session for every tool->platform call, so TLS behavior
    is controlled in a single place (INSECURE_SSL is for dev setups with
    self-signed LMS certificates; prefer REQUESTS_CA_BUNDLE)."""
    http_session = _TimeoutSession()
    if app.config.get("INSECURE_SSL"):
        http_session.verify = False
    return http_session


def get_launch_data_storage():
    return FlaskCacheDataStorage(cache)


def lti_base_url():
    return app_config.BASE_URL.rstrip("/") + "/lti"


# ------------------------------------------------------------------ launches

@blueprint.route("/login/", methods=["GET", "POST"])
def login():
    """OIDC third-party-initiated login. The iss/client_id/target_link_uri
    log lines below solve most integration failures - keep them."""
    flask_request = FlaskRequest()
    iss = flask_request.get_param("iss")
    client_id = flask_request.get_param("client_id")
    target_link_uri = flask_request.get_param("target_link_uri")
    app.logger.info(
        f"LTI OIDC login initiated ipaddr={get_remote_addr()} iss={iss} "
        f"client_id={client_id} target_link_uri={target_link_uri}"
    )
    if not target_link_uri:
        return "Missing target_link_uri parameter", 400
    try:
        oidc_login = FlaskOIDCLogin(
            flask_request, DbToolConf(), launch_data_storage=get_launch_data_storage()
        )
        response = oidc_login.enable_check_cookies().redirect(target_link_uri)
    # broad catch: besides LtiException/OIDCException, pylti1p3's tool config
    # raises plain Exception for unknown iss/client_id (the "iss not found in
    # settings" case) and that misconfiguration must not surface as a 500
    except Exception as exc:
        app.logger.warning(
            f"LTI OIDC login failed iss={iss} client_id={client_id}: {exc}"
        )
        return f"LTI login failed: {exc}", 400
    location = response.headers.get("Location") if hasattr(response, "headers") else None
    app.logger.info(
        f"LTI OIDC login redirect iss={iss} client_id={client_id} "
        f"location={location or 'cookie-check page'}"
    )
    return response


@blueprint.route("/launch/", methods=["POST"])
def launch():
    """LTI message launch: validate the id_token, provision/refresh the local
    user and open a regular dashboard session."""
    try:
        message_launch = FlaskMessageLaunch(
            FlaskRequest(),
            DbToolConf(),
            launch_data_storage=get_launch_data_storage(),
            requests_session=get_requests_session(),
        )
        launch_data = message_launch.get_launch_data()
    # broad catch for the same reason as in login()
    except Exception as exc:
        app.logger.warning(f"LTI launch failed ipaddr={get_remote_addr()}: {exc}")
        db.session.add(LoginLogging(
            ipaddr=get_remote_addr(), login="lti-launch", auth_provider="lti",
            success=False,
        ))
        db.session.commit()
        return f"LTI launch failed: {exc}", 403

    user = get_or_create_lti_user(launch_data)
    check_pre_approved(user)
    apply_lti_category(user, launch_data.get(LTI_ROLES_CLAIM))
    user.last_login = utcnow()
    db.session.add(LoginLogging(
        ipaddr=get_remote_addr(), login=launch_data.get("sub"),
        auth_provider="lti", success=True,
    ))
    db.session.commit()
    login_user(user)
    app.logger.info(
        f"Successful login ipaddr={get_remote_addr()} login={launch_data.get('sub')} "
        f"auth_provider=lti issuer={launch_data.get('iss')} email={user.email} "
        f"category={user.category}"
    )

    # deep-link support: set before the e-mail check so the destination
    # survives the /email/required detour (that flow pops session next_url)
    next_url = apply_custom_next_url(launch_data)
    # AGS grade-passback context (used when the user finishes a lab)
    store_launch_context(user, launch_data, next_url)
    db.session.commit()

    if not user.email:
        return redirect(url_for("authentication_blueprint.require_email"))
    if "next_url" in session:
        return redirect(session.pop("next_url"))
    return redirect(url_for("home_blueprint.index"))


def is_safe_redirect_url(url):
    """Accept only redirect targets that stay on this Dashboard: a relative
    path, or an absolute URL whose scheme and host:port exactly match
    BASE_URL. Everything else - external hosts, protocol-relative //host,
    javascript:, userinfo/lookalike-host and backslash tricks - is
    rejected. Never use prefix/substring matching here."""
    if not isinstance(url, str) or not url:
        return False
    if any(char == "\\" or char.isspace() or ord(char) < 0x20 for char in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if not parts.scheme and not parts.netloc:
        return url.startswith("/") and not url.startswith("//")
    base = urlsplit(app_config.BASE_URL)
    return (
        parts.scheme.lower() == base.scheme.lower()
        and parts.netloc.lower() == base.netloc.lower()
    )


def apply_custom_next_url(launch_data):
    """Platforms can send a custom parameter `next_url` (Moodle: the
    activity's "Custom parameters" field, `next_url=/labs/...`) through the
    LTI custom claim to deep-link the launch into a specific page. The value
    is attacker-influenceable, so only safe same-host targets are honored -
    stored in session["next_url"] (overwriting any stale value: the fresh
    launch intent wins); rejected values are logged and the launch proceeds
    to the default redirect. Returns the accepted next_url, or None."""
    issuer = launch_data.get("iss")
    custom_claims = launch_data.get(LTI_CUSTOM_CLAIM)
    if custom_claims is None:
        return None
    if not isinstance(custom_claims, dict):
        app.logger.warning(
            f"LTI launch custom claim ignored (not a dict) issuer={issuer} "
            f"value={custom_claims!r}"
        )
        return None
    next_url = custom_claims.get("next_url")
    if not next_url:
        return None
    if is_safe_redirect_url(next_url):
        session["next_url"] = next_url
        app.logger.info(
            f"LTI launch next_url accepted issuer={issuer} next_url={next_url}"
        )
        return next_url
    app.logger.warning(
        f"LTI launch next_url rejected (unsafe redirect) issuer={issuer} "
        f"next_url={next_url!r}"
    )
    return None


def store_launch_context(user, launch_data, next_url=None):
    """Persist the launch's AGS context (one row per user + LMS activity,
    refreshed on every launch) so grades can be sent back long after the
    launch, when the pylti1p3 cache is gone. Skipped when the platform did
    not send the AGS endpoint claim (activity without a grade service)."""
    ags_claim = launch_data.get(LTI_AGS_CLAIM)
    if not ags_claim or not isinstance(ags_claim, dict):
        return
    aud = launch_data.get("aud")
    client_id = aud[0] if isinstance(aud, (list, tuple)) else aud
    issuer = normalize_issuer(launch_data.get("iss"))
    resource_link = launch_data.get(LTI_RESOURCE_LINK_CLAIM) or {}
    resource_link_id = resource_link.get("id")
    context = LtiLaunchContext.query.filter_by(
        user_id=user.id, issuer=issuer, client_id=client_id,
        resource_link_id=resource_link_id,
    ).first()
    if not context:
        context = LtiLaunchContext(
            user_id=user.id, issuer=issuer, client_id=client_id,
            resource_link_id=resource_link_id,
        )
        db.session.add(context)
    context.deployment_id = launch_data.get(LTI_DEPLOYMENT_ID_CLAIM)
    context.ags = ags_claim
    context.custom_next_url = next_url
    # explicit bump: an unchanged re-launch must still count as most recent
    context.updated_at = utcnow()


def get_or_create_lti_user(launch_data):
    """First launch creates the Users row; later launches with the same
    (issuer, subject) inherit it and refresh the profile from the claims.
    LTI accounts have no password, so the local login form rejects them."""
    iss = normalize_issuer(launch_data.get("iss"))
    sub = launch_data.get("sub")
    user = Users.query.filter_by(issuer=iss, subject=sub, is_deleted=False).first()
    if not user:
        user = Users(
            subject=sub,
            issuer=iss,
            given_name=launch_data.get("given_name"),
            family_name=launch_data.get("family_name"),
            email=launch_data.get("email"),
        )
        db.session.add(user)
        db.session.flush()
    else:
        for attr in ("given_name", "family_name", "email"):
            value = launch_data.get(attr)
            if value:
                setattr(user, attr, value)
    return user


def apply_lti_category(user, roles):
    """Map LTI roles to a dashboard category, promotion-only: never demote
    an admin/labcreator, nor a teacher back to student."""
    roles = roles or []
    is_teacher = any("membership#Instructor" in role for role in roles)
    is_student = any("membership#Learner" in role for role in roles)
    if is_teacher and user.category in ("user", "student"):
        user.category = "teacher"
    elif is_student and user.category == "user":
        user.category = "student"


# ---------------------------------------------------------------------- jwks

@blueprint.route("/jwks/", methods=["GET"])
def jwks():
    """Public keyset: active keys plus retired keys still in their grace
    period (publish-then-switch rollover). Cached; rotation invalidates."""
    keys = cache.get(JWKS_CACHE_KEY)
    if keys is None:
        keys = build_jwks()
        cache.set(JWKS_CACHE_KEY, keys, timeout=JWKS_CACHE_TIMEOUT)
    return jsonify({"keys": keys})


# ---------------------------------------------------------------- registration

def _check_registration_credentials():
    """Returns (token_row, error). The endpoint is disabled unless a valid
    one-time token or the static key/secret pair is presented."""
    token_value = request.args.get("token", "")
    if token_value:
        token_row = LtiRegistrationToken.find_valid(token_value)
        if token_row:
            return token_row, None
        return None, ("Invalid, expired or already used registration token", 403)
    static_key = app.config.get("LTI_REGISTRATION_KEY") or ""
    static_secret = app.config.get("LTI_REGISTRATION_SECRET") or ""
    if static_key and static_secret:
        key = request.args.get("key", "")
        secret = request.args.get("secret", "")
        if hmac.compare_digest(key, static_key) and hmac.compare_digest(secret, static_secret):
            return None, None
        return None, ("Invalid registration credentials", 403)
    return None, (
        "Dynamic registration is disabled: no registration credentials "
        "configured (mint a token with 'flask lti mint-registration-token' "
        "or set LTI_REGISTRATION_KEY/LTI_REGISTRATION_SECRET)", 403,
    )


@blueprint.route("/register/", methods=["GET", "POST"])
def register():
    """LTI Dynamic Registration: the LMS admin pastes the credentialed URL
    into the platform (Moodle: Manage tools -> Add LTI Advantage); the
    platform calls back here with openid_configuration/registration_token,
    keeping our query-string credentials."""
    token_row, error = _check_registration_credentials()
    if error:
        app.logger.warning(
            f"LTI registration rejected ipaddr={get_remote_addr()}: {error[0]}"
        )
        return error

    openid_config_url = request.args.get("openid_configuration")
    registration_token = request.args.get("registration_token")
    if not openid_config_url:
        return "Missing openid_configuration parameter", 400

    http_session = get_requests_session()
    try:
        resp = http_session.get(openid_config_url, timeout=30)
        resp.raise_for_status()
        openid_config = resp.json()
    except Exception as exc:
        app.logger.warning(f"LTI registration: failed to fetch OpenID config from {openid_config_url}: {exc}")
        return f"Failed to fetch the platform OpenID configuration: {exc}", 502

    issuer = normalize_issuer(openid_config.get("issuer"))
    registration_endpoint = openid_config.get("registration_endpoint")
    if not issuer or not registration_endpoint:
        return "Platform OpenID configuration missing issuer/registration_endpoint", 502
    if not openid_config_url.startswith(issuer):
        app.logger.warning(
            f"LTI registration: openid_configuration URL {openid_config_url} "
            f"does not match advertised issuer {issuer}"
        )
        return "openid_configuration URL does not match the advertised issuer", 400

    # per-registration keypair, generated before registering so the platform
    # can fetch /lti/jwks/ right away; removed again if registration fails
    private_rel, public_rel = generate_keypair(issuer, "pending")
    cache.delete(JWKS_CACHE_KEY)

    login_url = lti_base_url() + "/login/"
    launch_url = lti_base_url() + "/launch/"
    tool_name = app.config.get("LTI_TOOL_NAME", "HackInSDN Dashboard")
    tool_logo = app.config.get("LTI_TOOL_LOGO") or ""
    if tool_logo and not tool_logo.startswith(("http://", "https://")):
        tool_logo = app_config.BASE_URL.rstrip("/") + tool_logo
    registration_payload = {
        "application_type": "web",
        "response_types": ["id_token"],
        "grant_types": ["client_credentials", "implicit"],
        "initiate_login_uri": login_url,
        "redirect_uris": [launch_url],
        "client_name": tool_name,
        "jwks_uri": lti_base_url() + "/jwks/",
        "token_endpoint_auth_method": "private_key_jwt",
        "scope": LTI_AGS_SCOPES,
        LTI_TOOL_CONF_CLAIM: {
            "domain": urlparse(app_config.BASE_URL).netloc,
            "target_link_uri": launch_url,
            "claims": ["iss", "sub", "name", "given_name", "family_name", "email"],
            "messages": [{
                "type": "LtiResourceLinkRequest",
                "target_link_uri": launch_url,
                "label": tool_name,
            }],
        },
    }
    if tool_logo:
        # icon the LMS shows for the tool (set LTI_TOOL_LOGO="" to disable)
        registration_payload["logo_uri"] = tool_logo
    headers = {"Content-Type": "application/json"}
    if registration_token:
        headers["Authorization"] = "Bearer " + registration_token

    try:
        resp = http_session.post(
            registration_endpoint, json=registration_payload,
            headers=headers, timeout=30,
        )
        resp.raise_for_status()
        reg_response = resp.json()
        client_id = reg_response["client_id"]
    except Exception as exc:
        delete_keypair(private_rel, public_rel)
        cache.delete(JWKS_CACHE_KEY)
        app.logger.warning(f"LTI registration failed against {registration_endpoint}: {exc}")
        return f"Platform rejected the client registration: {exc}", 502

    tool_conf_claim = reg_response.get(LTI_TOOL_CONF_CLAIM, {})
    deployment_id = tool_conf_claim.get("deployment_id")
    registration_entry = {
        "client_id": client_id,
        "auth_login_url": openid_config.get("authorization_endpoint"),
        "auth_token_url": openid_config.get("token_endpoint"),
        "auth_audience": None,
        "key_set_url": openid_config.get("jwks_uri"),
        "key_set": None,
        "deployment_ids": [deployment_id] if deployment_id else [],
        "private_key_file": private_rel,
        "public_key_file": public_rel,
    }

    row = LtiConfig.query.filter_by(issuer=issuer).first()
    if not row:
        row = LtiConfig(issuer=issuer)
        db.session.add(row)
    row.is_deleted = False
    old_registration = row.get_registration(client_id)
    row.upsert_registration(registration_entry)
    if token_row:
        token_row.used_at = utcnow()
    db.session.commit()

    # a re-registration with the same client_id rolls its previous keys over
    if old_registration:
        retire_keypair(
            old_registration.get("private_key_file"),
            old_registration.get("public_key_file"),
        )
    cache.delete(JWKS_CACHE_KEY)

    app.logger.info(
        f"LTI dynamic registration completed issuer={issuer} client_id={client_id} "
        f"deployment_ids={registration_entry['deployment_ids']}"
    )
    return REGISTRATION_CLOSE_PAGE


# ----------------------------------------------------------------------- CLI

@blueprint.cli.command("mint-registration-token")
@click.option("--label", required=True, help="Who this token is for (audit)")
@click.option("--ttl-hours", default=24, type=int, help="Token validity in hours")
def mint_registration_token(label, ttl_hours):
    """Mint a one-time dynamic registration token and print its URL."""
    token = secrets.token_urlsafe(32)
    db.session.add(LtiRegistrationToken(
        token_hash=LtiRegistrationToken.hash_token(token),
        label=label,
        expires_at=utcnow() + timedelta(hours=ttl_hours),
    ))
    db.session.commit()
    click.echo("Registration URL (shown once, hand it to the LMS admin):")
    click.echo(f"{lti_base_url()}/register/?token={token}")


@blueprint.cli.command("list-registration-tokens")
def list_registration_tokens():
    """List minted registration tokens (hashes only) for audit."""
    for row in LtiRegistrationToken.query.order_by(LtiRegistrationToken.id).all():
        click.echo(
            f"id={row.id} label={row.label!r} created={row.created_at} "
            f"expires={row.expires_at} used_at={row.used_at or '-'}"
        )


@blueprint.cli.command("rotate-key")
@click.option("--issuer", required=True, help="Platform issuer (LMS base URL)")
@click.option("--client-id", default=None, help="Registration to rotate (default: the issuer's default one)")
def rotate_key(issuer, client_id):
    """Rotate a registration's keypair (publish-then-switch): the new key is
    published in /lti/jwks/, the registration switches to it, and the old
    key is retired but stays published until purge-retired-keys."""
    row = LtiConfig.query.filter_by(issuer=normalize_issuer(issuer), is_deleted=False).first()
    if not row:
        raise click.ClickException(f"No lti_config entry for issuer {issuer}")
    registration = row.get_registration(client_id)
    if not registration:
        raise click.ClickException(f"No registration with client_id {client_id} for issuer {issuer}")

    old_private = registration.get("private_key_file")
    old_public = registration.get("public_key_file")
    private_rel, public_rel = generate_keypair(row.issuer, registration["client_id"])
    registration["private_key_file"] = private_rel
    registration["public_key_file"] = public_rel
    row.upsert_registration(registration)
    db.session.commit()
    retire_keypair(old_private, old_public)
    cache.delete(JWKS_CACHE_KEY)
    click.echo(
        f"Rotated key for issuer={row.issuer} client_id={registration['client_id']}: "
        f"new key {private_rel}, old key retired (still published in /lti/jwks/; "
        f"run 'flask lti purge-retired-keys' after the grace period)"
    )


@blueprint.cli.command("purge-retired-keys")
@click.option("--older-than-days", default=30, type=int, help="Grace period in days")
def purge_retired_keys_cmd(older_than_days):
    """Delete retired key files older than the grace period."""
    removed = purge_retired_keys(older_than_days)
    cache.delete(JWKS_CACHE_KEY)
    click.echo(f"Removed {len(removed)} retired key file(s)")
    for fname in removed:
        click.echo(f"  {fname}")
