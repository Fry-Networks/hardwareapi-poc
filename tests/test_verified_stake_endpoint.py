"""Regression test for FEM stake reporting (Run 4 / bug B3).

GET /credentials/{miner_key}/verified must report a device's active
verification stake so the Fry Edge Miner client can resolve its stake tier
against PoC.versions.stake_tiers. Before the fix the endpoint always returned
staked=null, so every FEM client rendered "No stake" regardless of the
device's real stake.

Runs against the live service. Requires HWAPI_TOKEN (shared bearer token).
"""
import json
import os
import urllib.request

import pytest

BASE = os.environ.get("HWAPI_BASE", "http://127.0.0.1:8084")
TOKEN = os.environ.get("HWAPI_TOKEN", "")
STAKED_KEY = os.environ.get("HWAPI_STAKED_KEY", "REDACTED_ROTATE_ME")


def _get(path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode())


@pytest.mark.skipif(not TOKEN, reason="HWAPI_TOKEN not set")
def test_verified_endpoint_reports_active_stake():
    status, body = _get("/credentials/%s/verified" % STAKED_KEY)
    assert status == 200, body
    staked = body.get("staked")
    assert staked is not None, "staked must be populated for a device with an active stake"
    assert staked.get("type") in ("24h", "6mo"), staked
    amount = staked.get("amount")
    assert isinstance(amount, (int, float)) and amount > 0, staked


@pytest.mark.skipif(not TOKEN, reason="HWAPI_TOKEN not set")
def test_stake_tier_key_resolves_in_versions_stake_tiers():
    _, version = _get("/versions/FEM")
    tiers = version.get("stake_tiers") or {}
    _, body = _get("/credentials/%s/verified" % STAKED_KEY)
    staked = body.get("staked") or {}
    assert staked.get("type") in tiers, (staked, sorted(tiers))
