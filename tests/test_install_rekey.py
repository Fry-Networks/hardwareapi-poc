"""Regression tests for installation re-keying (Run 5).

Installation documents used to be keyed by miner_key alone, so a second
installation of the same key overwrote the first one's device_token_hash,
install_id and lease fields — stranding the first client on HTTP 401 forever.
These tests drive the live API only.

Requires HWAPI_TOKEN (shared bearer token). Creates two installs of one
throwaway miner key and deletes them again in a fixture.
"""
import json
import os
import urllib.error
import urllib.request

import pytest

BASE = os.environ.get("HWAPI_BASE", "http://127.0.0.1:8084")
TOKEN = os.environ.get("HWAPI_TOKEN", "")
KEY = "FEM-" + "R5REKEY" + "A" * 25
INSTALL_A = "r5-install-a"
INSTALL_B = "r5-install-b"

pytestmark = pytest.mark.skipif(not TOKEN, reason="HWAPI_TOKEN not set")


def _call(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def _register(install_id):
    status, body = _call(
        "POST",
        "/installations/%s/installations/%s" % (KEY, install_id),
        TOKEN,
        {
            "miner_key": KEY,
            "install_id": install_id,
            "minerCode": "FEM",
            "software_version_installed": "0.0.0-r5test",
            "poc_version_installed": "1.0.0",
            "hostname": "r5-rekey-test",
            "os": "linux",
            "is_installed": True,
        },
    )
    assert status in (200, 202), (status, body)
    return body.get("device_token")


def _delete(install_id):
    return _call("DELETE", "/installations/%s/installations/%s" % (KEY, install_id), TOKEN)[0]


@pytest.fixture(scope="module", autouse=True)
def clean_installs():
    _delete(INSTALL_A)
    _delete(INSTALL_B)
    yield
    _delete(INSTALL_A)
    _delete(INSTALL_B)


def test_second_install_does_not_invalidate_first_device_token():
    token_a = _register(INSTALL_A)
    assert token_a, "registration must return a device token for install A"
    token_b = _register(INSTALL_B)
    assert token_b, "registration must return a device token for install B"
    assert token_a != token_b

    status_a, body_a = _call("GET", "/installations/%s/leases/current" % KEY, token_a)
    assert status_a == 200, ("install A's device token was invalidated by install B", status_a, body_a)

    status_b, body_b = _call("GET", "/installations/%s/leases/current" % KEY, token_b)
    assert status_b == 200, (status_b, body_b)


def test_lease_stays_exclusive_across_installs_of_one_key():
    _register(INSTALL_A)
    _register(INSTALL_B)

    status, granted = _call(
        "POST", "/installations/%s/leases/%s" % (KEY, INSTALL_A), TOKEN, {"lease_seconds": 120}
    )
    assert status == 200, (status, granted)
    assert granted.get("granted") is True, granted

    status, denied = _call(
        "POST", "/installations/%s/leases/%s" % (KEY, INSTALL_B), TOKEN, {"lease_seconds": 120}
    )
    assert status == 200, (status, denied)
    assert denied.get("granted") is False, ("a sibling install was granted a concurrent lease", denied)
    assert denied.get("holder_install_id") == INSTALL_A, denied

    status, renewed = _call(
        "PATCH", "/installations/%s/leases/%s" % (KEY, INSTALL_B), TOKEN, {"lease_seconds": 120}
    )
    assert status == 200, (status, renewed)
    assert renewed.get("granted") is False, ("a sibling install renewed over the holder's lease", renewed)


def test_lease_status_reports_the_actual_holder():
    _register(INSTALL_A)
    _register(INSTALL_B)
    _call("POST", "/installations/%s/leases/%s" % (KEY, INSTALL_A), TOKEN, {"lease_seconds": 120})

    status, body = _call("GET", "/installations/%s/leases/current" % KEY, TOKEN)
    assert status == 200, (status, body)
    assert body.get("holder_install_id") == INSTALL_A, body
