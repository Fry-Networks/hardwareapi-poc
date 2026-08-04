"""Regression: a FEM key must get a pending main.devices doc when it reaches the
server, via registration (upsert_installation) OR lease acquisition (acquire_lease).

Root cause (2026-08): acquire_lease/renew_lease upsert-created FEM installation
docs without the FEM dual-write, so a lease-first / registration-incomplete FEM
device was left with no main.devices doc (invisible + unclaimable on the
dashboard). Fixed by routing both paths through MongoStore._ensure_fem_device_doc.

Drives the live API. Requires HWAPI_TOKEN (shared bearer) + MONGODB_URI (read).
Optional ADMIN_MONGODB_URI cleans up the device doc (dbCredsAPI cannot delete
main.devices). Throwaway key is deleted in a fixture.
"""
import json
import os
import urllib.error
import urllib.request

import pytest
from pymongo import MongoClient

BASE = os.environ.get("HWAPI_BASE", "http://127.0.0.1:8084")
TOKEN = os.environ.get("HWAPI_TOKEN", "")
URI = os.environ.get("MONGODB_URI", "")
KEY = "FEM-QADWREGTEST" + "0" * 21  # FEM- + 32 chars
INSTALL = "dualwrite-regtest"

pytestmark = pytest.mark.skipif(not (TOKEN and URI), reason="HWAPI_TOKEN/MONGODB_URI not set")


def _post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _delete(path):
    req = urllib.request.Request(BASE + path, method="DELETE")
    req.add_header("Authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def cleanup():
    yield
    _delete("/installations/%s/installations/%s" % (KEY, INSTALL))
    admin = os.environ.get("ADMIN_MONGODB_URI")
    if admin:
        MongoClient(admin, tls=True, tlsAllowInvalidCertificates=True) \
            .get_database("main").get_collection("devices").delete_many({"miner_key": KEY})


def test_fem_registration_creates_pending_device_doc(cleanup):
    status = _post(
        "/installations/%s/installations/%s" % (KEY, INSTALL),
        {
            "miner_key": KEY,
            "install_id": INSTALL,
            "minerCode": "FEM",
            "software_version_installed": "0.0.0-dualwrite-test",
            "poc_version_installed": "1.0.0",
            "hostname": "dualwrite-regtest",
            "os": "linux",
            "is_installed": True,
        },
    )
    assert status in (200, 202), "registration returned %s" % status

    devices = MongoClient(URI, serverSelectionTimeoutMS=8000).get_database("main").get_collection("devices")
    doc = devices.find_one({"miner_key": KEY})
    assert doc is not None, "main.devices doc was NOT created for a FEM registration"
    assert doc.get("name") == "Fry Edge Miner"
    assert doc.get("is_registered") is False  # pending; dashboard claim flips this
