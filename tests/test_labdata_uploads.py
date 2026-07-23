"""Pytest suite for the Lab Data attachment feature.

Covers upload_labdata_file, serve_labdata and delete_labdata_file in
apps/home/routes.py: extension/size validation (enforced on the *encoded*
ConfigMap payload), the per-lab folder layout (labdata/<lab_id>/), lab_id
validation (path-traversal guard), pending vs. persisted metadata, labcreator
ownership gating and delete edge cases. The Kubernetes ConfigMap reconcile that
runs on lab *save* is covered by tests/test_k8s_controller.py; here the K8s
controller is not exercised.

Runs against a throwaway temporary SQLite database in a temp directory - never
touches the real dev/production DB. Files land in the temp DATA_DIR's uploads/.

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_labdata_uploads.py -v

Note: the classes below are ordered, stateful workflows - pytest runs methods in
definition order. Do not run with a random-order plugin without disabling it.

Seeded usernames are prefixed "ld" so they never collide with other modules that
share the same singleton app/database.
"""
import io
import os
import sys
import tempfile
import types

import pytest

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
from apps.authentication.models import Users  # noqa: E402
from apps.home.models import Labs, LabMetadata, generate_uuid  # noqa: E402

flask_app.config["TESTING"] = True
flask_app.config["WTF_CSRF_ENABLED"] = False


# --- fixtures ----------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _cleanup_temp_data_dir():
    yield
    import shutil

    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def app():
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()


@pytest.fixture(scope="session")
def client(app):
    return app.test_client()


@pytest.fixture(scope="session")
def ids(app):
    admin = Users(username="ldadmin", password="admin123", email="ldadmin@test.local", category="admin")
    creator = Users(username="ldcreator", password="lc123", email="ldcreator@test.local", category="labcreator")
    creator2 = Users(username="ldcreator2", password="lc123", email="ldcreator2@test.local", category="labcreator")
    student = Users(username="ldstudent", password="stud123", email="ldstudent@test.local", category="student")
    db.session.add_all([admin, creator, creator2, student])
    db.session.commit()

    owned = Labs(title="Owned Labdata Lab", description="owned by ldcreator")
    owned.updated_by = creator.id
    db.session.add(owned)
    db.session.commit()

    return {
        "admin_id": admin.id,
        "creator_id": creator.id,
        "creator2_id": creator2.id,
        "student_id": student.id,
        "owned_lab_id": owned.id,
    }


# --- helpers -----------------------------------------------------------
def login(client, username, password):
    resp = client.post("/login/", data={"identifier": username, "password": password, "login": "1"})
    return resp.status_code == 302


def logout(client):
    client.get("/logout")


def upload(client, filename, content=b"hello", lab_id=None):
    data = {"file": (io.BytesIO(content), filename)}
    if lab_id is not None:
        data["lab_id"] = lab_id
    return client.post("/labs/labdata/upload-file", data=data, content_type="multipart/form-data")


# --- upload_labdata_file -----------------------------------------------
class TestUploadLabdata:
    def test_student_is_rejected(self, client, ids):
        logout(client)
        login(client, "ldstudent", "stud123")
        resp = upload(client, "notes.txt", lab_id=generate_uuid())
        assert b"Unauthorized request" in resp.data
        logout(client)

    def test_invalid_lab_id_is_rejected(self, client, ids):
        login(client, "ldcreator", "lc123")
        resp = upload(client, "notes.txt", lab_id="../../etc")
        assert resp.status_code == 400
        assert b"Invalid or missing lab id" in resp.data

    def test_missing_file_part(self, client, ids):
        resp = client.post(
            "/labs/labdata/upload-file",
            data={"lab_id": generate_uuid()},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert b"No file part" in resp.data

    def test_disallowed_extension(self, client, ids):
        resp = upload(client, "malware.exe", lab_id=generate_uuid())
        assert resp.status_code == 400
        assert b"File extension not allowed" in resp.data

    def test_file_too_large(self, client, ids, monkeypatch):
        monkeypatch.setitem(flask_app.config, "LABDATA_UPLOAD_MAX_SIZE", 4)
        resp = upload(client, "big.txt", content=b"way too big", lab_id=generate_uuid())
        assert resp.status_code == 400
        assert b"too large" in resp.data

    def test_binary_size_uses_encoded_length(self, client, ids, monkeypatch):
        # 6 non-UTF-8 bytes -> base64 is 8 chars, so a limit of 7 must reject it
        monkeypatch.setitem(flask_app.config, "LABDATA_UPLOAD_MAX_SIZE", 7)
        resp = upload(client, "b.png", content=b"\xff\xfe\xff\xfe\xff\xfe", lab_id=generate_uuid())
        assert resp.status_code == 400
        assert b"too large" in resp.data

    def test_new_lab_upload_is_pending(self, client, ids):
        new_id = generate_uuid()
        resp = upload(client, "topo.yaml", content=b"nodes: []\n", lab_id=new_id)
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["status"] == "ok"
        entry = payload["entry"]
        assert entry["configmap_name"].startswith("labdata-")
        assert entry["original_name"] == "topo.yaml"
        # brand-new lab has no DB row -> nothing persisted, file on disk
        assert payload["labdata"] == []
        fpath = os.path.join(flask_app.config["UPLOAD_DIR"], "labdata", new_id, entry["filename"])
        assert os.path.exists(fpath)

    def test_existing_lab_upload_persists_metadata(self, client, ids):
        resp = upload(client, "data.conf", content=b"a=1\n", lab_id=ids["owned_lab_id"])
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["status"] == "ok"
        names = [e["original_name"] for e in payload["labdata"]]
        assert "data.conf" in names

        lab = db.session.get(Labs, ids["owned_lab_id"])
        cm_names = [e["configmap_name"] for e in lab.lab_metadata.md.get("labdata", [])]
        assert payload["entry"]["configmap_name"] in cm_names
        logout(client)

    def test_labcreator_non_owner_is_forbidden(self, client, ids):
        login(client, "ldcreator2", "lc123")
        resp = upload(client, "x.txt", lab_id=ids["owned_lab_id"])
        assert resp.status_code == 403
        logout(client)


# --- serve_labdata -----------------------------------------------------
class TestServeLabdata:
    def test_uploaded_file_is_served(self, client, ids):
        login(client, "ldcreator", "lc123")
        resp = upload(client, "served.txt", content=b"served-body", lab_id=ids["owned_lab_id"])
        entry = resp.get_json()["entry"]
        resp = client.get(f"/labs/{ids['owned_lab_id']}/labdata/{entry['filename']}")
        assert resp.status_code == 200
        assert resp.data == b"served-body"

    def test_invalid_lab_id_is_404(self, client, ids):
        resp = client.get("/labs/not-a-uuid/labdata/whatever.txt")
        assert resp.status_code == 404
        logout(client)


# --- delete_labdata_file -----------------------------------------------
class TestDeleteLabdata:
    def test_missing_lab_is_404(self, client, ids):
        login(client, "ldadmin", "admin123")
        resp = client.delete(f"/labs/{generate_uuid()}/labdata/x.txt")
        assert resp.status_code == 404

    def test_unknown_filename_is_404(self, client, ids):
        resp = client.delete(f"/labs/{ids['owned_lab_id']}/labdata/not-there.txt")
        assert resp.status_code == 404
        assert b"File not found in lab data list" in resp.data
        logout(client)

    def test_labcreator_non_owner_is_forbidden(self, client, ids):
        login(client, "ldcreator", "lc123")
        resp = upload(client, "todelete.txt", content=b"bye", lab_id=ids["owned_lab_id"])
        filename = resp.get_json()["entry"]["filename"]
        logout(client)

        login(client, "ldcreator2", "lc123")
        resp = client.delete(f"/labs/{ids['owned_lab_id']}/labdata/{filename}")
        assert resp.status_code == 403
        logout(client)

    def test_owner_can_delete(self, client, ids):
        login(client, "ldcreator", "lc123")
        resp = upload(client, "gone.txt", content=b"bye", lab_id=ids["owned_lab_id"])
        entry = resp.get_json()["entry"]
        fpath = os.path.join(flask_app.config["UPLOAD_DIR"], "labdata", ids["owned_lab_id"], entry["filename"])
        assert os.path.exists(fpath)

        resp = client.delete(f"/labs/{ids['owned_lab_id']}/labdata/{entry['filename']}")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert not os.path.exists(fpath)

        lab = db.session.get(Labs, ids["owned_lab_id"])
        filenames = [e["filename"] for e in lab.lab_metadata.md.get("labdata", [])]
        assert entry["filename"] not in filenames
        logout(client)
