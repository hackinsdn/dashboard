"""Pytest suite for the LTI 1.3 optional module (apps/lti).

Covers: key generation and JWKS assembly (active + retired keys, rotation
via the publish-then-switch flow, purge), the DB-backed pylti1p3 tool
configuration, the dynamic registration endpoint (credential gating with
one-time tokens and static key/secret, happy path against a faked platform,
orphan-key cleanup on failure, token consumption), and the launch flow
(user auto-provisioning keyed by issuer+subject, profile sync on later
launches, promotion-only role->category mapping, LoginLogging rows) plus
the OIDC login endpoint's diagnostic logging.

No LMS is required: the platform side is faked (requests session stub for
registration, a FlaskMessageLaunch stand-in for launches).

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3). Key files land in the temp DATA_DIR's lti/keys/.

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_lti.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "lti" so they never collide with the
other test modules that share the same singleton app/database (apps/config.py
reads DATA_DIR once at import time, so every module ends up on one DB).
"""
import json
import os
import stat
import sys
import tempfile
import types

import pytest
import requests as requests_lib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# --- isolate the test run from any real data --------------------------------
TEST_DATA_DIR = tempfile.mkdtemp(prefix="hackinsdn_test_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ.setdefault("OPTIONAL_MODULES", "")

# stub the clabernetes controller (needs a 'clabverter' binary at import time)
_fake_clabernetes = types.ModuleType("apps.controllers.clabernetes")


class _StubC9sController:
    def __getattr__(self, name):
        raise NotImplementedError("clabernetes stub - not needed for these tests")


_fake_clabernetes.C9sController = _StubC9sController
sys.modules["apps.controllers.clabernetes"] = _fake_clabernetes

from run import app as flask_app  # noqa: E402
from apps import db  # noqa: E402
from apps.authentication.models import LoginLogging, Users  # noqa: E402

# the lti module is optional and other test modules may have created the app
# with OPTIONAL_MODULES unset, so register it here if it isn't already
# (module import happens at pytest collection time, before any request)
from apps.lti import blueprint as lti_blueprint  # noqa: E402
import apps.lti.routes  # noqa: E402,F401  (attaches routes/CLI to the blueprint)
from apps.lti import keys as lti_keys  # noqa: E402
from apps.lti.models import LtiConfig, LtiRegistrationToken, normalize_issuer  # noqa: E402
from apps.lti.tool_conf import DbToolConf  # noqa: E402
from apps.audit_mixin import utcnow  # noqa: E402
from datetime import timedelta  # noqa: E402

if "lti_blueprint" not in flask_app.blueprints:
    flask_app.register_blueprint(lti_blueprint)

flask_app.config["TESTING"] = True
flask_app.config["WTF_CSRF_ENABLED"] = False

with flask_app.app_context():
    db.create_all()

ISSUER = "https://moodle.lti-test.example"
LTI_ROLES_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/roles"
LTI_TOOL_CONF_CLAIM = "https://purl.imsglobal.org/spec/lti-tool-configuration"
ROLE_LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
ROLE_INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def client():
    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            yield test_client


def _seed_registration(issuer=ISSUER, client_id="client-1"):
    """Create key files + an lti_config row like /lti/register/ would."""
    private_rel, public_rel = lti_keys.generate_keypair(issuer, client_id)
    row = LtiConfig.query.filter_by(issuer=normalize_issuer(issuer)).first()
    if not row:
        row = LtiConfig(issuer=issuer)
        db.session.add(row)
    row.upsert_registration({
        "client_id": client_id,
        "auth_login_url": issuer + "/mod/lti/auth.php",
        "auth_token_url": issuer + "/mod/lti/token.php",
        "auth_audience": None,
        "key_set_url": issuer + "/mod/lti/certs.php",
        "key_set": None,
        "deployment_ids": ["1"],
        "private_key_file": private_rel,
        "public_key_file": public_rel,
    })
    db.session.commit()
    return row


# --- keys + jwks --------------------------------------------------------------

class TestKeysAndJwks:
    def test_generate_keypair_writes_protected_files(self, client):
        private_rel, public_rel = lti_keys.generate_keypair(ISSUER, "kp-test")
        private_abs = lti_keys.abs_key_path(private_rel)
        assert os.path.isfile(private_abs)
        assert os.path.isfile(lti_keys.abs_key_path(public_rel))
        assert stat.S_IMODE(os.stat(private_abs).st_mode) == 0o600
        with open(private_abs) as f:
            assert "PRIVATE KEY" in f.read()
        lti_keys.delete_keypair(private_rel, public_rel)
        assert not os.path.exists(private_abs)

    def test_jwks_route_serves_active_keys(self, client):
        _seed_registration()
        lti_keys_cache_reset()
        resp = client.get("/lti/jwks/")
        assert resp.status_code == 200
        keyset = resp.get_json()
        assert len(keyset["keys"]) >= 1
        key = keyset["keys"][0]
        assert key["kty"] == "RSA"
        assert key["alg"] == "RS256"
        assert key["use"] == "sig"
        assert key["kid"]

    def test_rotate_key_publish_then_switch(self, client):
        row = LtiConfig.query.filter_by(issuer=ISSUER).first()
        old_reg = row.get_registration("client-1")
        old_public = old_reg["public_key_file"]
        with open(lti_keys.abs_key_path(old_public), "rb") as f:
            old_kid = lti_keys._jwk_from_pem(f.read())["kid"]

        runner = flask_app.test_cli_runner()
        result = runner.invoke(args=[
            "lti", "rotate-key", "--issuer", ISSUER + "/",  # trailing slash normalized
            "--client-id", "client-1",
        ])
        assert result.exception is None, result.output
        assert "Rotated key" in result.output

        db.session.expire_all()
        row = LtiConfig.query.filter_by(issuer=ISSUER).first()
        new_reg = row.get_registration("client-1")
        assert new_reg["public_key_file"] != old_public
        # old pair moved to retired/, new pair active
        assert not os.path.exists(lti_keys.abs_key_path(old_public))
        retired = os.listdir(lti_keys.retired_dir())
        assert os.path.basename(old_public) in retired

        # both kids stay published (grace period for cached tokens)
        resp = client.get("/lti/jwks/")
        kids = [k["kid"] for k in resp.get_json()["keys"]]
        assert old_kid in kids
        with open(lti_keys.abs_key_path(new_reg["public_key_file"]), "rb") as f:
            new_kid = lti_keys._jwk_from_pem(f.read())["kid"]
        assert new_kid in kids

    def test_purge_retired_keys_after_grace_period(self, client):
        runner = flask_app.test_cli_runner()
        result = runner.invoke(args=["lti", "purge-retired-keys", "--older-than-days", "0"])
        assert result.exception is None, result.output
        assert os.listdir(lti_keys.retired_dir()) == []
        resp = client.get("/lti/jwks/")
        kids = [k["kid"] for k in resp.get_json()["keys"]]
        row = LtiConfig.query.filter_by(issuer=ISSUER).first()
        assert len(kids) >= 1
        with open(lti_keys.abs_key_path(row.get_registration("client-1")["public_key_file"]), "rb") as f:
            assert lti_keys._jwk_from_pem(f.read())["kid"] in kids


def lti_keys_cache_reset():
    from apps import cache
    cache.delete(lti_keys.JWKS_CACHE_KEY)


# --- tool conf ----------------------------------------------------------------

class TestDbToolConf:
    def test_registration_lookup_and_keys(self, client):
        tool_conf = DbToolConf()
        registration = tool_conf.find_registration_by_params(ISSUER, "client-1")
        assert registration.get_client_id() == "client-1"
        assert "PRIVATE KEY" in registration.get_tool_private_key()
        deployment = tool_conf.find_deployment_by_params(ISSUER, "1", "client-1")
        assert deployment is not None

    def test_issuer_normalized_on_write(self, client):
        row = LtiConfig(issuer="https://other.lti-test.example///")
        assert row.issuer == "https://other.lti-test.example"
        with pytest.raises(ValueError):
            LtiConfig(issuer="/")


# --- dynamic registration -------------------------------------------------------

class _FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.url = "https://faked/"
        self.text = json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests_lib.HTTPError(f"{self.status_code}", response=self)


REG_ISSUER = "https://reg.lti-test.example"
OPENID_CONFIG_URL = REG_ISSUER + "/.well-known/openid-configuration"
OPENID_CONFIG = {
    "issuer": REG_ISSUER,
    "authorization_endpoint": REG_ISSUER + "/mod/lti/auth.php",
    "token_endpoint": REG_ISSUER + "/mod/lti/token.php",
    "jwks_uri": REG_ISSUER + "/mod/lti/certs.php",
    "registration_endpoint": REG_ISSUER + "/mod/lti/openid-registration.php",
}


class _FakeSession:
    """requests.Session stand-in for the platform side of registration."""
    def __init__(self, reg_status=200, client_id="dyn-client-9"):
        self.reg_status = reg_status
        self.client_id = client_id
        self.posted = None

    def get(self, url, **kwargs):
        assert url == OPENID_CONFIG_URL
        return _FakeResponse(OPENID_CONFIG)

    def post(self, url, json=None, headers=None, **kwargs):
        self.posted = {"url": url, "json": json, "headers": headers}
        if self.reg_status >= 400:
            return _FakeResponse({"error": "denied"}, self.reg_status)
        return _FakeResponse({
            "client_id": self.client_id,
            LTI_TOOL_CONF_CLAIM: {"deployment_id": "dep-7"},
        })


class TestDynamicRegistration:
    def test_disabled_without_credentials(self, client):
        flask_app.config["LTI_REGISTRATION_KEY"] = ""
        flask_app.config["LTI_REGISTRATION_SECRET"] = ""
        resp = client.get("/lti/register/", query_string={
            "openid_configuration": OPENID_CONFIG_URL,
        })
        assert resp.status_code == 403
        assert b"disabled" in resp.data

    def test_invalid_token_rejected(self, client):
        resp = client.get("/lti/register/", query_string={
            "token": "not-a-real-token",
            "openid_configuration": OPENID_CONFIG_URL,
        })
        assert resp.status_code == 403

    def test_static_credentials_and_happy_path(self, client, monkeypatch):
        flask_app.config["LTI_REGISTRATION_KEY"] = "reg-key"
        flask_app.config["LTI_REGISTRATION_SECRET"] = "reg-secret"
        fake_session = _FakeSession()
        monkeypatch.setattr("apps.lti.routes.get_requests_session", lambda: fake_session)

        # wrong secret first
        resp = client.get("/lti/register/", query_string={
            "key": "reg-key", "secret": "wrong",
            "openid_configuration": OPENID_CONFIG_URL,
        })
        assert resp.status_code == 403

        resp = client.get("/lti/register/", query_string={
            "key": "reg-key", "secret": "reg-secret",
            "openid_configuration": OPENID_CONFIG_URL,
            "registration_token": "platform-reg-token",
        })
        assert resp.status_code == 200
        assert b"org.imsglobal.lti.close" in resp.data

        # registration request sent to the platform
        assert fake_session.posted["url"] == OPENID_CONFIG["registration_endpoint"]
        assert fake_session.posted["headers"]["Authorization"] == "Bearer platform-reg-token"
        payload = fake_session.posted["json"]
        assert payload["initiate_login_uri"].endswith("/lti/login/")
        assert payload["redirect_uris"][0].endswith("/lti/launch/")
        assert payload["jwks_uri"].endswith("/lti/jwks/")
        assert payload["token_endpoint_auth_method"] == "private_key_jwt"

        # config row upserted with the platform's client/deployment ids
        row = LtiConfig.query.filter_by(issuer=REG_ISSUER).first()
        assert row is not None
        reg = row.get_registration("dyn-client-9")
        assert reg["default"] is True
        assert reg["deployment_ids"] == ["dep-7"]
        assert reg["auth_login_url"] == OPENID_CONFIG["authorization_endpoint"]
        assert reg["auth_token_url"] == OPENID_CONFIG["token_endpoint"]
        assert reg["key_set_url"] == OPENID_CONFIG["jwks_uri"]
        # per-registration keypair generated and readable
        assert os.path.isfile(lti_keys.abs_key_path(reg["private_key_file"]))
        assert os.path.isfile(lti_keys.abs_key_path(reg["public_key_file"]))
        flask_app.config["LTI_REGISTRATION_KEY"] = ""
        flask_app.config["LTI_REGISTRATION_SECRET"] = ""

    def test_one_time_token_consumed_on_success(self, client, monkeypatch):
        monkeypatch.setattr(
            "apps.lti.routes.get_requests_session",
            lambda: _FakeSession(client_id="dyn-client-10"),
        )
        token = "one-time-lti-token"
        db.session.add(LtiRegistrationToken(
            token_hash=LtiRegistrationToken.hash_token(token),
            label="test", expires_at=utcnow() + timedelta(hours=1),
        ))
        db.session.commit()

        resp = client.get("/lti/register/", query_string={
            "token": token, "openid_configuration": OPENID_CONFIG_URL,
        })
        assert resp.status_code == 200
        row = LtiRegistrationToken.query.filter_by(
            token_hash=LtiRegistrationToken.hash_token(token)).first()
        assert row.used_at is not None

        # reuse is rejected
        resp = client.get("/lti/register/", query_string={
            "token": token, "openid_configuration": OPENID_CONFIG_URL,
        })
        assert resp.status_code == 403

    def test_expired_token_rejected(self, client):
        token = "expired-lti-token"
        db.session.add(LtiRegistrationToken(
            token_hash=LtiRegistrationToken.hash_token(token),
            label="test", expires_at=utcnow() - timedelta(hours=1),
        ))
        db.session.commit()
        resp = client.get("/lti/register/", query_string={
            "token": token, "openid_configuration": OPENID_CONFIG_URL,
        })
        assert resp.status_code == 403

    def test_platform_rejection_keeps_token_and_removes_keys(self, client, monkeypatch):
        monkeypatch.setattr(
            "apps.lti.routes.get_requests_session",
            lambda: _FakeSession(reg_status=403),
        )
        token = "retryable-lti-token"
        db.session.add(LtiRegistrationToken(
            token_hash=LtiRegistrationToken.hash_token(token),
            label="test", expires_at=utcnow() + timedelta(hours=1),
        ))
        db.session.commit()
        keys_before = set(os.listdir(lti_keys.keys_dir()))

        resp = client.get("/lti/register/", query_string={
            "token": token, "openid_configuration": OPENID_CONFIG_URL,
        })
        assert resp.status_code == 502
        # orphan keypair removed, token NOT consumed (failure is retryable)
        assert set(os.listdir(lti_keys.keys_dir())) == keys_before
        row = LtiRegistrationToken.query.filter_by(
            token_hash=LtiRegistrationToken.hash_token(token)).first()
        assert row.used_at is None

    def test_mismatched_openid_config_url_rejected(self, client, monkeypatch):
        monkeypatch.setattr(
            "apps.lti.routes.get_requests_session", lambda: _FakeSession(),
        )
        flask_app.config["LTI_REGISTRATION_KEY"] = "reg-key"
        flask_app.config["LTI_REGISTRATION_SECRET"] = "reg-secret"

        class _EvilSession(_FakeSession):
            def get(self, url, **kwargs):
                return _FakeResponse(OPENID_CONFIG)

        monkeypatch.setattr("apps.lti.routes.get_requests_session", lambda: _EvilSession())
        resp = client.get("/lti/register/", query_string={
            "key": "reg-key", "secret": "reg-secret",
            "openid_configuration": "https://attacker.example/.well-known/openid-configuration",
        })
        assert resp.status_code == 400
        assert b"does not match" in resp.data
        flask_app.config["LTI_REGISTRATION_KEY"] = ""
        flask_app.config["LTI_REGISTRATION_SECRET"] = ""


# --- launch: provisioning + role mapping -----------------------------------------

class _FakeMessageLaunch:
    launch_data = {}
    error = None

    def __init__(self, *args, **kwargs):
        pass

    def get_launch_data(self):
        if self.error:
            raise self.error
        return dict(self.launch_data)


def _launch_claims(sub, roles, email=None, given_name="Ada", family_name="Lovelace"):
    claims = {
        "iss": ISSUER,
        "sub": sub,
        "given_name": given_name,
        "family_name": family_name,
        LTI_ROLES_CLAIM: roles,
    }
    if email:
        claims["email"] = email
    return claims


class TestLaunchProvisioning:
    @pytest.fixture(autouse=True)
    def _fake_launch(self, monkeypatch):
        monkeypatch.setattr("apps.lti.routes.FlaskMessageLaunch", _FakeMessageLaunch)
        yield
        _FakeMessageLaunch.error = None

    def test_first_launch_creates_student(self, client):
        _FakeMessageLaunch.launch_data = _launch_claims(
            "lti-sub-student", [ROLE_LEARNER], email="lti-student@example.com")
        resp = client.post("/lti/launch/")
        assert resp.status_code == 302
        assert "/lti/" not in resp.headers["Location"]

        user = Users.query.filter_by(issuer=ISSUER, subject="lti-sub-student").first()
        assert user is not None
        assert user.category == "student"
        assert user.email == "lti-student@example.com"
        assert user.given_name == "Ada"
        assert user.password is None
        assert user.last_login is not None
        log = LoginLogging.query.filter_by(
            auth_provider="lti", login="lti-sub-student").first()
        assert log is not None and log.success is True

    def test_second_launch_inherits_account_and_syncs(self, client):
        _FakeMessageLaunch.launch_data = _launch_claims(
            "lti-sub-student", [ROLE_LEARNER],
            email="lti-student-new@example.com", given_name="Augusta")
        resp = client.post("/lti/launch/")
        assert resp.status_code == 302
        users = Users.query.filter_by(issuer=ISSUER, subject="lti-sub-student").all()
        assert len(users) == 1
        assert users[0].email == "lti-student-new@example.com"
        assert users[0].given_name == "Augusta"

    def test_teacher_role_maps_to_teacher(self, client):
        _FakeMessageLaunch.launch_data = _launch_claims(
            "lti-sub-teacher", [ROLE_INSTRUCTOR], email="lti-teacher@example.com")
        client.post("/lti/launch/")
        user = Users.query.filter_by(issuer=ISSUER, subject="lti-sub-teacher").first()
        assert user.category == "teacher"

    def test_student_promoted_to_teacher_but_never_demoted(self, client):
        # the student from the first test now launches as Instructor
        _FakeMessageLaunch.launch_data = _launch_claims(
            "lti-sub-student", [ROLE_INSTRUCTOR], email="lti-student-new@example.com")
        client.post("/lti/launch/")
        user = Users.query.filter_by(issuer=ISSUER, subject="lti-sub-student").first()
        assert user.category == "teacher"

        # ... and a later Learner launch does not demote them
        _FakeMessageLaunch.launch_data = _launch_claims(
            "lti-sub-student", [ROLE_LEARNER], email="lti-student-new@example.com")
        client.post("/lti/launch/")
        user = Users.query.filter_by(issuer=ISSUER, subject="lti-sub-student").first()
        assert user.category == "teacher"

    def test_admin_never_demoted(self, client):
        admin = Users(
            username="lti_admin", email="lti-admin@example.com",
            category="admin", issuer=ISSUER, subject="lti-sub-admin",
        )
        db.session.add(admin)
        db.session.commit()
        _FakeMessageLaunch.launch_data = _launch_claims(
            "lti-sub-admin", [ROLE_LEARNER], email="lti-admin@example.com")
        client.post("/lti/launch/")
        user = Users.query.filter_by(issuer=ISSUER, subject="lti-sub-admin").first()
        assert user.category == "admin"

    def test_launch_without_email_redirects_to_require_email(self, client):
        _FakeMessageLaunch.launch_data = _launch_claims(
            "lti-sub-noemail", [ROLE_LEARNER], email=None)
        resp = client.post("/lti/launch/")
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/email/required"

    def test_invalid_launch_logs_failure(self, client):
        from pylti1p3.exception import LtiException
        _FakeMessageLaunch.error = LtiException("Invalid id_token")
        resp = client.post("/lti/launch/")
        assert resp.status_code == 403
        log = LoginLogging.query.filter_by(
            auth_provider="lti", success=False).first()
        assert log is not None


# --- custom claim next_url (deep-link redirect) -----------------------------------

class TestCustomNextUrl:
    BASE_URL = None  # set in _fake_launch from the app config

    @pytest.fixture(autouse=True)
    def _fake_launch(self, monkeypatch):
        monkeypatch.setattr("apps.lti.routes.FlaskMessageLaunch", _FakeMessageLaunch)
        from apps.config import app_config
        type(self).BASE_URL = app_config.BASE_URL.rstrip("/")
        yield
        _FakeMessageLaunch.error = None

    def _launch_with_custom(self, client, custom, sub="lti-sub-nexturl",
                            email="lti-nexturl@example.com"):
        claims = _launch_claims(sub, [ROLE_LEARNER], email=email)
        if custom is not None:
            claims["https://purl.imsglobal.org/spec/lti/claim/custom"] = custom
        _FakeMessageLaunch.launch_data = claims
        return client.post("/lti/launch/")

    def test_no_custom_claim_keeps_default_redirect(self, client):
        resp = self._launch_with_custom(client, None)
        assert resp.status_code == 302
        default_location = resp.headers["Location"]

        # claim present but without next_url behaves the same
        resp = self._launch_with_custom(client, {"other_param": "x"})
        assert resp.headers["Location"] == default_location

    def test_relative_next_url_redirects(self, client):
        resp = self._launch_with_custom(client, {"next_url": "/labs/abc?tab=2"})
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/labs/abc?tab=2"
        with client.session_transaction() as sess:
            assert "next_url" not in sess

    def test_same_host_absolute_next_url_redirects(self, client):
        target = self.BASE_URL + "/labs/abc"
        resp = self._launch_with_custom(client, {"next_url": target})
        assert resp.status_code == 302
        assert resp.headers["Location"] == target

    def test_unsafe_next_urls_rejected(self, client, caplog):
        base_host = self.BASE_URL.split("://", 1)[1]
        resp = self._launch_with_custom(client, None)
        default_location = resp.headers["Location"]
        unsafe = [
            "https://evil.example/x",
            "//evil.example/x",
            "/\\evil.example/x",
            "javascript:alert(1)",
            "labs/abc",                                 # not /-prefixed
            "",
            ["/labs/abc"],                              # non-string
            f"https://{base_host}@evil.example/",       # userinfo trick
            f"https://{base_host}.evil.example/",       # lookalike subdomain
            f"https://{base_host}:8443/x",              # port swap
            self.BASE_URL.replace("https://", "http://") + "/x",  # scheme downgrade
        ]
        for value in unsafe:
            with caplog.at_level("WARNING"):
                caplog.clear()
                resp = self._launch_with_custom(client, {"next_url": value})
            assert resp.status_code == 302, value
            assert resp.headers["Location"] == default_location, value
            if value:  # empty/missing values are silently ignored, not logged
                assert "next_url rejected" in caplog.text, value

    def test_non_dict_custom_claim_rejected(self, client, caplog):
        resp = self._launch_with_custom(client, None)
        default_location = resp.headers["Location"]
        with caplog.at_level("WARNING"):
            resp = self._launch_with_custom(client, "next_url=/labs/abc")
        assert resp.headers["Location"] == default_location
        assert "custom claim ignored" in caplog.text

    def test_custom_next_url_overrides_stale_session_value(self, client):
        with client.session_transaction() as sess:
            sess["next_url"] = "/stale/destination"
        resp = self._launch_with_custom(client, {"next_url": "/labs/fresh"})
        assert resp.headers["Location"] == "/labs/fresh"

    def test_next_url_survives_require_email_detour(self, client):
        resp = self._launch_with_custom(
            client, {"next_url": "/labs/abc"},
            sub="lti-sub-nexturl-noemail", email=None)
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/email/required"
        with client.session_transaction() as sess:
            assert sess.get("next_url") == "/labs/abc"


# --- OIDC login ------------------------------------------------------------------

class TestOidcLogin:
    def test_missing_target_link_uri(self, client):
        resp = client.get("/lti/login/")
        assert resp.status_code == 400

    def test_unknown_issuer_logged_and_rejected(self, client, caplog):
        with caplog.at_level("INFO"):
            resp = client.post("/lti/login/", data={
                "iss": "https://unknown.lms.example",
                "client_id": "nope",
                "login_hint": "1",
                "target_link_uri": "https://dashboard.example/lti/launch/",
                # what the pylti1p3 cookie-check page re-posts, so the login
                # proceeds to issuer validation instead of serving that page
                "lti1p3_new_window": "1",
            })
        assert resp.status_code == 400
        assert "iss=https://unknown.lms.example" in caplog.text
        assert "target_link_uri=" in caplog.text

    def test_cookie_check_page_then_redirect_to_platform(self, client):
        login_data = {
            "iss": ISSUER,
            "client_id": "client-1",
            "login_hint": "42",
            "target_link_uri": "https://dashboard.example/lti/launch/",
        }
        # first hit serves the cookie-check page (enable_check_cookies)...
        resp = client.post("/lti/login/", data=login_data)
        assert resp.status_code == 200
        assert b"lti1p3_new_window" in resp.data

        # ...which re-posts with lti1p3_new_window=1 and gets the OIDC
        # redirect to the platform's auth endpoint, state/nonce included
        resp = client.post("/lti/login/", data=dict(login_data, lti1p3_new_window="1"))
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert location.startswith(ISSUER + "/mod/lti/auth.php")
        assert "state=" in location and "nonce=" in location
        assert "client_id=client-1" in location
