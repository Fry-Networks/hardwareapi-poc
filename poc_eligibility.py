"""
Phase 4: reward_eligible computation for INSTALLER PoC.hardware documents.

Server-computes a boolean `reward_eligible` flag on PUT /PoC/{miner_key}/hardware.
dbrewards (ARES00) reads this flag as a strict `=== false` short-circuit for INSTALLER devices.

Design:
- INSTALLER allowlist: MinerCode enum (BM, IDM, ODM, ISM, OSM, RDN, AEM).
- NON_INSTALLER (prefix not in enum): skip-write entirely (no field added).
- INSTALLER eligibility requires ALL three gates to pass:
    (1) poc_uptodate : document["software"]["poc_version_installed"] == PoC.versions[miner_code].poc_version_needed
    (2) liveness_ok  : now_utc - parse(document["lastUpdated"]) in [0, POC_LIVENESS_STALENESS_SECONDS]
    (3) past_cutoff  : now_utc >= parse(POC_LIVENESS_CUTOFF_DATE)
- Fail-closed on any of:
    - POC_LIVENESS_CUTOFF_DATE unset, empty, or unparseable
    - now < cutoff
    - miner_code not in PoC.versions (unknown INSTALLER type)
    - poc_version_needed missing from versions doc
    - software.poc_version_installed missing from request
    - version strings mismatch
    - lastUpdated missing or unparseable
    - negative age (lastUpdated in future) or age > staleness window
"""
import os
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from models import MinerCode

_INSTALLER_MINER_CODES = frozenset(m.value for m in MinerCode)

_POC_LIVENESS_CUTOFF_DATE_RAW = os.environ.get("POC_LIVENESS_CUTOFF_DATE", "").strip()
try:
    _POC_LIVENESS_STALENESS_SECONDS = int(os.environ.get("POC_LIVENESS_STALENESS_SECONDS", "86400"))
except (ValueError, TypeError):
    _POC_LIVENESS_STALENESS_SECONDS = 86400


def _parse_iso(raw):
    if not raw or not isinstance(raw, str):
        return None
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


_POC_LIVENESS_CUTOFF_DT = _parse_iso(_POC_LIVENESS_CUTOFF_DATE_RAW)


def extract_miner_code(miner_key):
    if not miner_key or not isinstance(miner_key, str):
        return ""
    if "-" in miner_key:
        return miner_key.split("-", 1)[0].upper()
    return miner_key[:3].upper()


def compute_reward_eligible(document, miner_key, get_version_for_miner_code):
    """
    Returns (should_write: bool, eligible: bool).
      should_write=False -> caller must NOT add reward_eligible field (NON_INSTALLER).
      should_write=True, eligible=False -> caller sets reward_eligible=False.
      should_write=True, eligible=True  -> caller sets reward_eligible=True.
    """
    miner_code = extract_miner_code(miner_key)
    if miner_code not in _INSTALLER_MINER_CODES:
        return (False, False)

    # Cutoff / kill switch
    if _POC_LIVENESS_CUTOFF_DT is None:
        return (True, False)
    now = datetime.now(timezone.utc)
    if now < _POC_LIVENESS_CUTOFF_DT:
        return (True, False)

    # Version lookup
    try:
        version_doc = get_version_for_miner_code(miner_code)
    except Exception:
        return (True, False)
    if not version_doc:
        return (True, False)
    # Required poc_version: try OS-nested first (BM/AEM windows shape per recon),
    # then top-level (for miner types whose versions docs are flat, e.g. RDN/SDN/SVN).
    required = None
    software_in = document.get("software") if isinstance(document, dict) else None
    if isinstance(software_in, dict):
        os_key = software_in.get("os")
        if os_key and isinstance(version_doc.get(os_key), dict):
            required = version_doc[os_key].get("poc_version_needed")
    if not required:
        required = version_doc.get("poc_version_needed")
    if not required:
        return (True, False)

    # Client-sent installed version (nested in software subdoc)
    software = document.get("software") if isinstance(document, dict) else None
    if not isinstance(software, dict):
        return (True, False)
    installed = software.get("poc_version_installed")
    if not installed:
        return (True, False)
    if str(installed).strip() != str(required).strip():
        return (True, False)

    # Liveness gate
    last_updated_dt = _parse_iso(document.get("lastUpdated"))
    if last_updated_dt is None:
        return (True, False)
    age = (now - last_updated_dt).total_seconds()
    if age < 0 or age > _POC_LIVENESS_STALENESS_SECONDS:
        return (True, False)

    return (True, True)
