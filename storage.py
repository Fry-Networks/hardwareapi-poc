from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple
import os
import sys
import logging

# Import measurement aggregator (lives in the same directory as storage.py)
try:
    from measurement_aggregator import get_aggregator
    HAS_AGGREGATOR = True
except Exception as e:
    logging.warning(f"Measurement aggregator not available: {e}")
    HAS_AGGREGATOR = False
    get_aggregator = None  # type: ignore

try:
    from pymongo import MongoClient
    from pymongo import ReturnDocument
    from pymongo.collection import Collection
except Exception:  # pragma: no cover - optional dependency
    MongoClient = None  # type: ignore
    class _RD:  # pragma: no cover - executed when pymongo missing
        AFTER = True

    ReturnDocument = _RD  # type: ignore

@dataclass
class InstallationRecord:
    miner_key: str
    install_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

@dataclass
class LeaseRecord:
    miner_key: str
    holder_install_id: str
    expires_at: datetime
    last_seen_at: datetime
    history_payload: Dict[str, Any] = field(default_factory=dict)
    external_ip: Optional[str] = None

    def ttl_seconds(self) -> int:
        return max(0, int((self.expires_at - datetime.now(timezone.utc)).total_seconds()))


class InMemoryStore:
    """Thread-safe in-memory backing store for the external API."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._versions: Dict[str, Dict[str, Any]] = {}
        self._miner_profiles: Dict[str, Dict[str, Any]] = {}
        self._installations: Dict[Tuple[str, str], InstallationRecord] = {}
        self._leases: Dict[str, LeaseRecord] = {}
        self._hardware_docs: Dict[str, Dict[str, Any]] = {}
        self._measurements: Dict[str, Dict[str, Any]] = {}  # keyed by hex_id
        self._mysterium: Dict[str, Dict[str, Any]] = {}
        self._presearch: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Version metadata
    def get_version_document(self, miner_code: str) -> Optional[Dict[str, Any]]:
        """Return the full version document for a miner code."""
        with self._lock:
            data = self._versions.get(miner_code.upper())
            return self._coerce_version_sections(dict(data)) if data is not None else None

    def get_required_version(self, miner_code: str, platform: str = "windows") -> Optional[Dict[str, Optional[str]]]:
        """Compatibility helper that returns software/poc versions for a single platform."""
        normalized = self._normalize_platform_key(platform)
        doc = self.get_version_document(miner_code)
        if doc is None:
            return None
        return self._select_versions_by_platform(doc, normalized)

    def update_version_document(self, miner_code: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Merge platform-specific version updates into the stored document."""
        limit_value = updates.get("limit")
        normalized_updates = self._normalize_updates(updates)
        if not normalized_updates and limit_value is None:
            raise ValueError("No version fields provided")

        with self._lock:
            existing = dict(self._versions.get(miner_code.upper()) or {})
            merged = self._merge_version_doc(existing, normalized_updates)
            merged["miner_code"] = miner_code
            if limit_value is not None:
                merged["limit"] = limit_value
            self._versions[miner_code.upper()] = merged
            return self._coerce_version_sections(dict(merged))

    def set_required_version(self, miner_code: str, software_version: Optional[str] = None, poc_version: Optional[str] = None) -> None:
        """Compatibility wrapper to update Windows version requirements."""
        payload: Dict[str, Any] = {"windows": {}}
        if software_version is not None:
            payload["windows"]["software_version_needed"] = software_version
        if poc_version is not None:
            payload["windows"]["poc_version_needed"] = poc_version
        self.update_version_document(miner_code, payload)

    def list_supported_installers(self, os_name: str) -> List[str]:
        """Return miner codes that have installer metadata for the requested OS."""
        normalized = (os_name or "").strip().lower()
        if normalized not in ("linux", "windows", "test-linux", "test-windows"):
            raise ValueError("OS must be one of: 'linux', 'windows', 'test-linux', 'test-windows'")

        with self._lock:
            supported: List[str] = []
            for code, metadata in self._versions.items():
                if self._has_installer(metadata, normalized):
                    supported.append(code)

        return sorted(supported)

    @staticmethod
    def _has_installer(metadata: Dict[str, Any], platform: str) -> bool:
        platform_key = InMemoryStore._normalize_platform_key(platform)
        section = InMemoryStore._coerce_version_sections(metadata).get(platform_key) or {}
        if not isinstance(section, dict):
            return False
        return any(InMemoryStore._has_value(section.get(field)) for field in ("software_version_needed", "poc_version_needed"))

    @staticmethod
    def _merge_version_doc(existing: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(existing)
        for platform_key, payload in updates.items():
            platform_key = InMemoryStore._normalize_platform_key(platform_key)
            if payload is None:
                continue
            if not isinstance(payload, dict):
                continue
            current = dict(merged.get(platform_key) or {})
            for field in ("software_version_needed", "poc_version_needed"):
                if payload.get(field) is not None:
                    current[field] = payload[field]
            if current:
                merged[platform_key] = current
        return merged

    @staticmethod
    def _select_versions_by_platform(metadata: Dict[str, Any], platform: str) -> Dict[str, Optional[str]]:
        """Select software/poc versions for requested platform. Returns {} when unavailable."""
        platform_key = InMemoryStore._normalize_platform_key(platform)
        section = metadata.get(platform_key) or {}
        if not isinstance(section, dict):
            return {}
        software = InMemoryStore._first_non_empty(section, ("software_version_needed",))
        poc = InMemoryStore._first_non_empty(section, ("poc_version_needed",))
        result: Dict[str, Optional[str]] = {}
        if software:
            result["software_version"] = software
        if poc:
            result["poc_version"] = poc
        return result

    @staticmethod
    def _first_non_empty(metadata: Dict[str, Any], fields: Tuple[str, ...]) -> Optional[str]:
        for field in fields:
            value = metadata.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _normalize_platform_key(platform: str) -> str:
        normalized = (platform or "").strip().lower().replace("_", "-")
        if normalized not in ("windows", "linux", "test-windows", "test-linux"):
            raise ValueError("Unsupported platform")
        return normalized

    @staticmethod
    def _normalize_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        for key, value in updates.items():
            if key in ("miner_code",):
                continue
            try:
                platform_key = InMemoryStore._normalize_platform_key(key)
            except ValueError:
                continue
            normalized[platform_key] = value
        return normalized

    @staticmethod
    def _has_value(value: Any) -> bool:
        return value is not None and value != ""

    @staticmethod
    def _coerce_version_sections(doc: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure legacy flat fields are mapped into platform sections."""
        coerced = dict(doc)

        windows_section = dict(coerced.get("windows") or {})
        linux_section = dict(coerced.get("linux") or {})

        # Fill Windows section from legacy fields if missing
        if not InMemoryStore._has_value(windows_section.get("software_version_needed")):
            legacy = InMemoryStore._first_non_empty(coerced, ("software_version_needed", "software_version"))
            if legacy:
                windows_section["software_version_needed"] = legacy
        if not InMemoryStore._has_value(windows_section.get("poc_version_needed")):
            legacy = InMemoryStore._first_non_empty(coerced, ("poc_version_needed", "poc_version"))
            if legacy:
                windows_section["poc_version_needed"] = legacy
        if windows_section:
            coerced["windows"] = windows_section

        # Fill Linux section from legacy fields if missing
        if not InMemoryStore._has_value(linux_section.get("software_version_needed")):
            legacy = InMemoryStore._first_non_empty(coerced, ("linux_software_version_needed", "linux_software_version"))
            if legacy:
                linux_section["software_version_needed"] = legacy
        if not InMemoryStore._has_value(linux_section.get("poc_version_needed")):
            legacy = InMemoryStore._first_non_empty(coerced, ("linux_poc_version_needed", "linux_poc_version", "poc_version_needed", "poc_version"))
            if legacy:
                linux_section["poc_version_needed"] = legacy
        if linux_section:
            coerced["linux"] = linux_section

        return coerced

    # ------------------------------------------------------------------
    # Rewards parameters
    def update_rewards_params(self, miner_code: str, multiplier_base: float, multiplier_per_tool: float) -> None:
        """Set BM-style rewards multiplier parameters on the version document."""
        with self._lock:
            existing = self._versions.setdefault(miner_code.upper(), {})
            existing["multiplier_base"] = multiplier_base
            existing["multiplier_per_tool"] = multiplier_per_tool

    # ------------------------------------------------------------------
    # Miner credentials
    def get_miner_profile(self, miner_key: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._miner_profiles.get(miner_key, {"exists": False}))

    def set_miner_profile(self, miner_key: str, **fields: Any) -> None:
        with self._lock:
            profile = self._miner_profiles.setdefault(miner_key, {"exists": False})
            profile.update(fields)
            profile["exists"] = True

    # ------------------------------------------------------------------
    # Installation heartbeats
    def upsert_installation(self, miner_key: str, install_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            rec = InstallationRecord(miner_key=miner_key, install_id=install_id, payload=dict(payload))
            self._installations[(miner_key, install_id)] = rec

    def find_conflicting_miner_key_by_external_ip(self, miner_code_prefix: str, external_ip: str) -> Optional[str]:
        """Return a conflicting full miner_key (str) for the given external_ip when an
        active lease exists whose miner_key starts with miner_code_prefix (e.g., 'BM'),
        or None if no conflict.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            for mk, rec in self._leases.items():
                if mk.startswith(miner_code_prefix) and rec.external_ip == external_ip and rec.expires_at > now:
                    return mk
        return None

    def find_installations_by_external_ip(self, external_ip: str) -> List[Dict[str, Any]]:
        """Return all active installations (leases) associated with the given external_ip."""
        now = datetime.now(timezone.utc)
        results: List[Dict[str, Any]] = []
        with self._lock:
            for mk, rec in self._leases.items():
                if rec.external_ip == external_ip and rec.expires_at > now:
                    results.append({
                        "miner_key": mk,
                        "install_id": rec.holder_install_id,
                        "lease_expires_at": rec.expires_at.isoformat(),
                        "last_seen_at": rec.last_seen_at.isoformat(),
                    })
        return results

    def delete_installation(self, miner_key: str, install_id: str) -> bool:
        """Delete an installation record. Returns True if deleted, False if not found."""
        with self._lock:
            key = (miner_key, install_id)
            if key in self._installations:
                del self._installations[key]
                # Also clean up any lease for this installation
                if miner_key in self._leases:
                    lease = self._leases[miner_key]
                    if lease.holder_install_id == install_id:
                        del self._leases[miner_key]
                return True
            return False

    # ------------------------------------------------------------------
    # Leases
    def acquire_lease(self, miner_key: str, install_id: str, lease_seconds: int, external_ip: Optional[str] = None) -> Tuple[bool, LeaseRecord]:
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(seconds=max(lease_seconds, 1))
        with self._lock:
            current = self._leases.get(miner_key)
            if current:
                if current.holder_install_id != install_id and current.expires_at > now:
                    return False, current
            # BM one-per-IP enforcement: check if another active BM lease uses this IP
            if external_ip and miner_key.startswith("BM"):
                for mk, rec in self._leases.items():
                    if mk == miner_key:
                        continue
                    if mk.startswith("BM") and rec.external_ip == external_ip and rec.expires_at > now:
                        # Return a sentinel record with the conflicting miner_key
                        return False, rec
            record = LeaseRecord(
                miner_key=miner_key,
                holder_install_id=install_id,
                expires_at=expiry,
                last_seen_at=now,
                external_ip=external_ip,
            )
            self._leases[miner_key] = record
            return True, record

    def renew_lease(self, miner_key: str, install_id: str, lease_seconds: int, external_ip: Optional[str] = None) -> Tuple[bool, Optional[LeaseRecord]]:
        now = datetime.now(timezone.utc)
        with self._lock:
            current = self._leases.get(miner_key)
            if not current or current.holder_install_id != install_id:
                return False, current
            current.expires_at = now + timedelta(seconds=max(lease_seconds, 1))
            current.last_seen_at = now
            if external_ip is not None:
                current.external_ip = external_ip
            return True, current

    def lease_status(self, miner_key: str) -> Dict[str, Any]:
        with self._lock:
            record = self._leases.get(miner_key)
            if not record:
                return {"active": False, "holder_install_id": None, "expires_at": None, "ttl_seconds": 0}
            return {
                "active": record.expires_at > datetime.now(timezone.utc),
                "holder_install_id": record.holder_install_id,
                "expires_at": record.expires_at.isoformat(),
                "ttl_seconds": record.ttl_seconds(),
            }

    # ------------------------------------------------------------------
    # Hardware aggregates
    def get_hardware_doc(self, miner_key: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._hardware_docs.get(miner_key, {}))

    def put_hardware_doc(self, miner_key: str, document: Dict[str, Any]) -> None:
        with self._lock:
            self._hardware_docs[miner_key] = dict(document)

    # ------------------------------------------------------------------
    # Presearch
    def put_presearch(self, ip: str, document: Dict[str, Any]) -> None:
        with self._lock:
            existing = self._presearch.get(ip)
            if existing and "nodes" in existing and "nodes" in document:
                # Index existing nodes by node_key for fast lookup
                merged = {n["node_key"]: n for n in existing["nodes"]}
                for node in document["nodes"]:
                    merged[node["node_key"]] = node
                doc = dict(document)
                doc["nodes"] = list(merged.values())
                self._presearch[ip] = doc
            else:
                self._presearch[ip] = dict(document)

    # ------------------------------------------------------------------
    # Measurements
    def upload_measurement(self, hex_id: str, miner_code: str, install_id: str, timestamp: str, measurement_type: str, value: Dict[str, Any]) -> None:
        """Store a measurement using daily aggregates (in-memory version).
        
        Aggregates measurements by date with min/max/avg statistics,
        drastically reducing storage size (99%+ reduction).
        """
        with self._lock:
            # Get or create hex document
            existing_doc = self._measurements.get(hex_id)
            
            # Use aggregator if available, otherwise fall back to raw storage
            if HAS_AGGREGATOR and get_aggregator:
                aggregator = get_aggregator()
                updated_doc = aggregator.process_measurement_upload(
                    hex_id=hex_id,
                    timestamp=timestamp,
                    measurement_type=measurement_type,
                    value=value,
                    existing_doc=existing_doc
                )
                self._measurements[hex_id] = updated_doc
            else:
                # Fallback: raw storage (legacy behavior)
                if existing_doc is None:
                    self._measurements[hex_id] = {"hex_id": hex_id}
                
                doc = self._measurements[hex_id]
                
                # Parse timestamp
                ts_dt: Optional[datetime] = None
                try:
                    ts_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except Exception:
                    ts_dt = None
                
                # Create measurement entry
                entry = {
                    "timestamp": timestamp,
                    "timestamp_dt": ts_dt,
                    "miner_code": miner_code,
                    "install_id": install_id,
                    "value": dict(value),
                    "uploaded_at": datetime.now(timezone.utc),
                }
                
                # Append to measurement type array
                if measurement_type not in doc:
                    doc[measurement_type] = []
                doc[measurement_type].append(entry)

    def get_measurements(self, hex_id: str, start: Optional[datetime], end: Optional[datetime], measurement_types: Optional[List[str]]) -> Dict[str, Any]:
        """Get all measurements for a hex, optionally filtered by date range and measurement types."""
        with self._lock:
            doc = self._measurements.get(hex_id)
            if not doc:
                return {"hex_id": hex_id}
            
            result: Dict[str, Any] = {"hex_id": hex_id}
            
            # Process each measurement type array
            for key, value in doc.items():
                if key == "hex_id":
                    continue
                # Skip if measurement_types filter is set and this type isn't in it
                if measurement_types and key not in measurement_types:
                    continue
                # Filter array by date range if specified
                if isinstance(value, list):
                    filtered = []
                    for item in value:
                        if not isinstance(item, dict):
                            continue
                        ts_dt = item.get("timestamp_dt")
                        if start and (not isinstance(ts_dt, datetime) or ts_dt < start):
                            continue
                        if end and (not isinstance(ts_dt, datetime) or ts_dt > end):
                            continue
                        # Remove internal fields for cleaner response
                        clean_item = {k: v for k, v in item.items() if k not in ("timestamp_dt", "uploaded_at")}
                        filtered.append(clean_item)
                    if filtered:
                        result[key] = filtered
            
            return result

    # ------------------------------------------------------------------
    # Mysterium keystores
    def upsert_mysterium_keystore(self, miner_key: str, keystore_b64: str, identity_id: Optional[str]) -> Dict[str, Any]:
        """Store or update a Mysterium keystore blob for a miner key."""
        with self._lock:
            record = {
                "miner_key": miner_key,
                "keystore_b64": keystore_b64,
                "identity_id": identity_id,
                "updated_at": datetime.now(timezone.utc),
            }
            self._mysterium[miner_key] = record
            return dict(record)

    def get_mysterium_keystore(self, miner_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._mysterium.get(miner_key)
            return dict(record) if record else None


class MongoStore:
    """MongoDB-backed store. Uses environment variables:
    - MONGODB_URI (mongodb connection string)
    
    Uses fixed database names: 'PoC' for installations/hardware, 'creds' for credentials.
    Requires MONGODB_URI environment variable and pymongo to be installed.
    """
    def __init__(self) -> None:
        self._lock = RLock()
        uri = os.environ.get("MONGODB_URI")
        
        if MongoClient is None:
            raise ImportError("pymongo is required for MongoStore. Install with: pip install pymongo")
        
        if not uri:
            raise ValueError("MONGODB_URI environment variable is required for MongoStore")
            
        # No fallback - proceed with MongoDB initialization

        self._client = MongoClient(uri, serverSelectionTimeoutMS=5000)

        # Always use PoC database for versions, installations, and hardware
        poc_db = self._client.get_database("PoC")
        self._versions: Collection = poc_db.get_collection("versions")
        self._installations: Collection = poc_db.get_collection("installations")
        self._hardware_docs: Collection = poc_db.get_collection("hardware")
        
        # Measurements: separate database with one collection per country
        # Database: "measurements"
        # Collections: "France", "Germany", "United_Kingdom", "United_States", etc.
        self._measurements_db = self._client.get_database("measurements")
        self._country_collections: Dict[str, Collection] = {}  # Cache for country collections
        
        self._mysterium: Collection = poc_db.get_collection("mysterium")
        self._presearch: Collection = poc_db.get_collection("presearch")

        try:
            self._mysterium.create_index([("miner_key", 1)], unique=True)
        except Exception:
            # Non-fatal if index creation fails
            pass

        # Always use creds database for miner profiles/credentials
        creds_db = self._client.get_database("creds")
        self._miner_profiles: Collection = creds_db.get_collection("hardware")
    
        # Fallback: main database devices (admin panel records)
        main_db = self._client.get_database("main")
        self._main_devices: Collection = main_db.get_collection("devices")
    
    def _get_measurements_collection(self, country: str) -> Collection:
        """Get or create measurements collection for a specific country.
        
        Returns a collection in the 'measurements' database named after the country.
        E.g., 'France', 'Germany', 'United_Kingdom', etc.
        """
        # Sanitize country name for collection naming
        safe_country = country.replace(' ', '_').replace('/', '_').replace('.', '_')
        
        if safe_country not in self._country_collections:
            collection = self._measurements_db.get_collection(safe_country)
            
            try:
                # Create index for efficient queries by hex_id
                collection.create_index([("hex_id", 1)], unique=True)
            except Exception:
                pass  # Non-fatal if index creation fails
            
            self._country_collections[safe_country] = collection
        
        return self._country_collections[safe_country]



    # No fallback mechanism - MongoDB is required

    # Version metadata
    def get_version_document(self, miner_code: str) -> Optional[Dict[str, Any]]:
        doc = self._versions.find_one({"miner_code": miner_code})
        if doc is None:
            return None
        return self._normalize_version_doc(doc)

    def get_required_version(self, miner_code: str, platform: str = "windows") -> Optional[Dict[str, Optional[str]]]:
        normalized = self._normalize_platform_key(platform)
        doc = self.get_version_document(miner_code)
        if doc is None:
            return None
        return self._select_versions_by_platform(doc, normalized)

    def update_version_document(self, miner_code: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        limit_value = updates.get("limit")
        normalized_updates = self._normalize_updates(updates)
        if not normalized_updates and limit_value is None:
            raise ValueError("No version fields provided")

        set_fields: Dict[str, Any] = {"miner_code": miner_code}
        for platform_key, payload in normalized_updates.items():
            if payload is None or not isinstance(payload, dict):
                continue
            for field in ("software_version_needed", "poc_version_needed"):
                if payload.get(field) is not None:
                    set_fields[f"{platform_key}.{field}"] = payload[field]
        if limit_value is not None:
            set_fields["limit"] = limit_value

        updated = self._versions.find_one_and_update(
            {"miner_code": miner_code},
            {"$set": set_fields},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        return self._normalize_version_doc(updated or {})

    def set_required_version(self, miner_code: str, software_version: Optional[str] = None, poc_version: Optional[str] = None) -> None:
        """Compatibility wrapper to update Windows version requirements."""
        payload: Dict[str, Any] = {"windows": {}}
        if software_version is not None:
            payload["windows"]["software_version_needed"] = software_version
        if poc_version is not None:
            payload["windows"]["poc_version_needed"] = poc_version
        self.update_version_document(miner_code=miner_code, updates=payload)

    def list_supported_installers(self, os_name: str) -> List[str]:
        """Query versions collection for miner codes with installer metadata for the given OS."""
        normalized = (os_name or "").strip().lower()
        if normalized not in ("linux", "windows", "test-linux", "test-windows"):
            raise ValueError("OS must be one of: 'linux', 'windows', 'test-linux', 'test-windows'")

        # Map the OS name to the document field key
        field_prefix = normalized.replace("-", "-")  # test-windows stays as test-windows
        
        # Check for the platform-specific fields in the document
        query = {f"{field_prefix}.software_version_needed": {"$exists": True, "$nin": [None, ""]}}
        cursor = self._versions.find(query, {"_id": 0, "miner_code": 1})
        codes = {doc.get("miner_code") for doc in cursor if doc.get("miner_code")}

        return sorted(codes)

    @staticmethod
    def _select_versions_by_platform(doc: Dict[str, Any], platform: str) -> Dict[str, Optional[str]]:
        platform_key = MongoStore._normalize_platform_key(platform)
        section = doc.get(platform_key) or {}
        if not isinstance(section, dict):
            return {}
        software = MongoStore._first_non_empty(section, ("software_version_needed",))
        poc = MongoStore._first_non_empty(section, ("poc_version_needed",))

        result: Dict[str, Optional[str]] = {}
        if software:
            result["software_version"] = software
        if poc:
            result["poc_version"] = poc
        return result

    @staticmethod
    def _normalize_version_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = dict(doc or {})
        if "_id" in cleaned:
            try:
                cleaned["_id"] = str(cleaned["_id"]) if cleaned["_id"] is not None else None
            except Exception:
                pass
        return MongoStore._coerce_version_sections(cleaned)

    @staticmethod
    def _first_non_empty(doc: Dict[str, Any], fields: Tuple[str, ...]) -> Optional[str]:
        for field in fields:
            value = doc.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _normalize_platform_key(platform: str) -> str:
        normalized = (platform or "").strip().lower().replace("_", "-")
        if normalized not in ("windows", "linux", "test-windows", "test-linux"):
            raise ValueError("Unsupported platform")
        return normalized

    @staticmethod
    def _normalize_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        for key, value in updates.items():
            if key in ("miner_code",):
                continue
            try:
                platform_key = MongoStore._normalize_platform_key(key)
            except ValueError:
                continue
            normalized[platform_key] = value
        return normalized

    @staticmethod
    def _coerce_version_sections(doc: Dict[str, Any]) -> Dict[str, Any]:
        """Map legacy flat version fields into platform sections when needed."""
        coerced = dict(doc)

        windows_section = dict(coerced.get("windows") or {})
        linux_section = dict(coerced.get("linux") or {})

        if not MongoStore._first_non_empty(windows_section, ("software_version_needed",)):
            legacy = MongoStore._first_non_empty(coerced, ("software_version_needed", "software_version"))
            if legacy:
                windows_section["software_version_needed"] = legacy
        if not MongoStore._first_non_empty(windows_section, ("poc_version_needed",)):
            legacy = MongoStore._first_non_empty(coerced, ("poc_version_needed", "poc_version"))
            if legacy:
                windows_section["poc_version_needed"] = legacy
        if windows_section:
            coerced["windows"] = windows_section

        if not MongoStore._first_non_empty(linux_section, ("software_version_needed",)):
            legacy = MongoStore._first_non_empty(coerced, ("linux_software_version_needed", "linux_software_version"))
            if legacy:
                linux_section["software_version_needed"] = legacy
        if not MongoStore._first_non_empty(linux_section, ("poc_version_needed",)):
            legacy = MongoStore._first_non_empty(coerced, ("linux_poc_version_needed", "linux_poc_version", "poc_version_needed", "poc_version"))
            if legacy:
                linux_section["poc_version_needed"] = legacy
        if linux_section:
            coerced["linux"] = linux_section

        return coerced

    # Rewards parameters
    def update_rewards_params(self, miner_code: str, multiplier_base: float, multiplier_per_tool: float) -> None:
        """Set BM-style rewards multiplier parameters on the version document."""
        self._versions.find_one_and_update(
            {"miner_code": miner_code},
            {"$set": {"multiplier_base": multiplier_base, "multiplier_per_tool": multiplier_per_tool}},
            upsert=True,
        )

    # Miner profiles - always use creds.hardware collection
    def get_miner_profile(self, miner_key: str) -> Dict[str, Any]:
        try:
            # First try miner_profiles collection
            doc = self._miner_profiles.find_one({"miner_key": miner_key}) or self._miner_profiles.find_one({"minerKey": miner_key})
            if doc:
                doc = dict(doc)
                doc.pop("_id", None)
                doc.setdefault("exists", True)
                return doc
            
            # If not found, try installations collection
            doc = self._installations.find_one({"miner_key": miner_key})
            if doc:
                doc = dict(doc)
                doc.pop("_id", None)
                doc.setdefault("exists", True)
                return doc
            
            # If not found, try hardware collection
            doc = self._hardware_docs.find_one({"miner_key": miner_key}) or self._hardware_docs.find_one({"minerKey": miner_key})
            if doc:
                doc = dict(doc)
                doc.pop("_id", None)
                doc.setdefault("exists", True)
                return doc
            
            # Fallback 4: Check main.devices (admin panel device records)
            doc = self._main_devices.find_one({"miner_key": miner_key}) or self._main_devices.find_one({"minerKey": miner_key})
            if doc:
                doc = dict(doc)
                doc.pop("_id", None)
                doc.setdefault("exists", True)
                return doc
        except Exception:
            pass

        return {"exists": False}

    def set_miner_profile(self, miner_key: str, **fields: Any) -> None:
        payload = dict(fields)
        payload.setdefault("exists", True)
        payload["miner_key"] = miner_key
        self._miner_profiles.update_one({"miner_key": miner_key}, {"$set": payload}, upsert=True)

    # Installations
    def upsert_installation(self, miner_key: str, install_id: str, payload: Dict[str, Any]) -> None:
        key = {"miner_key": miner_key, "install_id": install_id}
        payload_copy = dict(payload)
        now = datetime.now(timezone.utc)
        # Map incoming fields to your existing installation document shape
        set_on_insert = {
            "first_installed_at": now,
            "_lease": False,
        }
        update_doc: Dict[str, Any] = {"$set": {}, "$setOnInsert": set_on_insert}
        
        # Extract version fields directly
        software_version = payload_copy.get("software_version_installed")
        poc_version = payload_copy.get("poc_version_installed")
        software_needed = payload_copy.get("software_version_needed")
        poc_needed = payload_copy.get("poc_version_needed")
        
        # canonical fields
        update_doc["$set"].update({
            "miner_key": miner_key,
            "install_id": install_id,
            "last_seen_at": payload_copy.get("last_seen_at") or now,
            "hostname": payload_copy.get("hostname"),
            "os": payload_copy.get("os"),
            "software_version_installed": software_version,
            "poc_version_installed": poc_version,
            "software_version_needed": software_needed,
            "poc_version_needed": poc_needed,
            "is_installed": payload_copy.get("is_installed", True),
            "minerCode": payload_copy.get("minerCode"),
        })
        # merge any other keys from payload (keep schema flexible)
        for k, v in payload_copy.items():
            if k not in ("miner_key", "install_id"):
                update_doc["$set"][k] = v
        # Prevent overwriting first_installed_at
        if "first_installed_at" in update_doc["$set"]:
            del update_doc["$set"]["first_installed_at"]
        # Remove any keys that would update subpaths of first_installed_at
        to_del = [k for k in list(update_doc["$set"].keys()) if k.startswith("first_installed_at.")]
        for k in to_del:
            del update_doc["$set"][k]

        self._installations.update_one(key, update_doc, upsert=True)

    def find_conflicting_miner_key_by_external_ip(self, miner_code_prefix: str, external_ip: str) -> Optional[str]:
        """Mongo-backed lookup for a conflicting miner_key by external_ip.

        Only considers active leases (lease_expires_at > now).
        Returns the first matching miner_key (string) or None if none found.
        """
        now = datetime.now(timezone.utc)
        try:
            q = {
                "miner_key": {"$regex": f"^{miner_code_prefix}"},
                "external_ip": external_ip,
                "lease_expires_at": {"$gt": now},
            }
            doc = self._installations.find_one(q, {"miner_key": 1})
            if doc:
                return doc.get("miner_key")
        except Exception:
            pass
        return None

    def find_installations_by_external_ip(self, external_ip: str) -> List[Dict[str, Any]]:
        """Return all active installations associated with the given external_ip."""
        now = datetime.now(timezone.utc)
        results: List[Dict[str, Any]] = []
        try:
            cursor = self._installations.find(
                {"external_ip": external_ip, "lease_expires_at": {"$gt": now}},
                {"_id": 0, "miner_key": 1, "install_id": 1, "minerCode": 1,
                 "lease_expires_at": 1, "last_seen_at": 1, "hostname": 1, "os": 1,
                 "software_version_installed": 1, "poc_version_installed": 1},
            )
            for doc in cursor:
                entry = dict(doc)
                # Serialize datetimes
                for dt_field in ("lease_expires_at", "last_seen_at"):
                    val = entry.get(dt_field)
                    if isinstance(val, datetime):
                        entry[dt_field] = val.isoformat()
                results.append(entry)
        except Exception:
            pass
        return results

    def delete_installation(self, miner_key: str, install_id: str) -> bool:
        """Delete an installation record. Returns True if deleted, False if not found."""
        result = self._installations.delete_one({"miner_key": miner_key, "install_id": install_id})
        return result.deleted_count > 0

    # Leases
    def acquire_lease(self, miner_key: str, install_id: str, lease_seconds: int, external_ip: Optional[str] = None) -> Tuple[bool, LeaseRecord]:
        # Single atomic conditional find_one_and_update that grants a lease only when:
        # - no active lease exists (lease_expires_at missing or <= now)
        # - the document is for this install_id
        # This eliminates the race window between check and update.
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(seconds=max(lease_seconds, 1))

        # BM one-per-IP enforcement: check if another active BM lease uses this IP
        is_bm = miner_key.startswith("BM")
        if is_bm and external_ip:
            conflict = self._installations.find_one({
                "miner_key": {"$regex": "^BM", "$ne": miner_key},
                "external_ip": external_ip,
                "lease_expires_at": {"$gt": now},
            })
            if conflict:
                print(f"[MongoStore] acquire_lease: IP_ALREADY_REGISTERED ({external_ip}) held by {conflict.get('miner_key')}")
                rec = LeaseRecord(
                    miner_key=conflict.get("miner_key", "unknown"),
                    holder_install_id=conflict.get("lease_install_id", "unknown"),
                    expires_at=conflict.get("lease_expires_at", now),
                    last_seen_at=conflict.get("last_seen_at", now),
                    external_ip=external_ip,
                )
                return False, rec

        # Prepare filter for the atomic operation
        # The $or condition ensures we only update when:
        # 1. This install_id already holds the lease (renew/extend), OR
        # 2. No lease exists or lease is expired
        filter_q = {
            "miner_key": miner_key,
            "install_id": install_id,
            "$or": [
                {"lease_install_id": install_id},  # This install already holds it
                {"lease_expires_at": {"$lte": now}},  # Expired
                {"lease_expires_at": {"$exists": False}}  # No lease
            ]
        }

        update_doc = {
            "$set": {
                "lease_expires_at": expiry,
                "lease_install_id": install_id,
                "last_seen_at": now,
                "_lease": True,
            },
            "$setOnInsert": {"first_installed_at": now},
        }
        # Persist external_ip for all miners (used for per-IP installation tracking)
        if external_ip:
            update_doc["$set"]["external_ip"] = external_ip

        try:
            # Atomic conditional update: only succeeds if no active lease or lease expired
            updated = self._installations.find_one_and_update(
                filter_q,
                update_doc,
                upsert=True,
                return_document=ReturnDocument.AFTER
            )

            if updated:
                print(f"[MongoStore] acquire_lease: atomic grant succeeded for {miner_key}/{install_id}")

                expires_val = updated.get("lease_expires_at")
                last_seen = updated.get("last_seen_at", now)
                return True, LeaseRecord(miner_key=miner_key, holder_install_id=install_id, expires_at=expires_val, last_seen_at=last_seen, external_ip=updated.get("external_ip"))

            # If updated is None, it means the condition failed (another active lease exists)
            print(f"[MongoStore] acquire_lease: atomic grant DENIED (active lease exists) for {miner_key}/{install_id}")
            # Find who currently holds the lease
            current_holder = self._installations.find_one(
                {"miner_key": miner_key, "lease_expires_at": {"$gt": now}}
            )
            if current_holder:
                rec = LeaseRecord(
                    miner_key=miner_key,
                    holder_install_id=current_holder.get("lease_install_id", "unknown"),
                    expires_at=current_holder.get("lease_expires_at"),
                    last_seen_at=current_holder.get("last_seen_at", now)
                )
                return False, rec

            # Edge case: no document returned but no active holder found either
            return False, LeaseRecord(miner_key=miner_key, holder_install_id="unknown", expires_at=now, last_seen_at=now)

        except Exception as e:
            # On any error, fall back to best-effort non-atomic path
            print(f"[MongoStore] acquire_lease: FALLBACK to non-atomic (error: {e}) for {miner_key}/{install_id}")
            fallback_filter = {"miner_key": miner_key, "install_id": install_id}
            fallback_set = {
                "lease_expires_at": expiry,
                "lease_install_id": install_id,
                "last_seen_at": now,
                "_lease": True
            }
            if external_ip:
                fallback_set["external_ip"] = external_ip
            try:
                self._installations.update_one(
                    fallback_filter,
                    {"$set": fallback_set},
                    upsert=True
                )
            except Exception:
                pass
            return True, LeaseRecord(miner_key=miner_key, holder_install_id=install_id, expires_at=expiry, last_seen_at=now, external_ip=external_ip)

    def renew_lease(self, miner_key: str, install_id: str, lease_seconds: int, external_ip: Optional[str] = None) -> Tuple[bool, Optional[LeaseRecord]]:
        now = datetime.now(timezone.utc)
        new_expiry = now + timedelta(seconds=max(lease_seconds, 1))

        # Atomic conditional renewal: only succeeds if this install_id currently holds the lease
        filter_q = {
            "miner_key": miner_key,
            "install_id": install_id,
            "lease_install_id": install_id
        }

        update_set: Dict[str, Any] = {"lease_expires_at": new_expiry, "last_seen_at": now}
        if external_ip is not None:
            update_set["external_ip"] = external_ip

        try:
            updated = self._installations.find_one_and_update(
                filter_q,
                {"$set": update_set},
                return_document=ReturnDocument.AFTER
            )

            if updated:
                print(f"[MongoStore] renew_lease: atomic renewal succeeded for {miner_key}/{install_id}")
                return True, LeaseRecord(miner_key=miner_key, holder_install_id=install_id, expires_at=new_expiry, last_seen_at=now, external_ip=updated.get("external_ip"))

            # Atomic renewal failed - not the current holder or doc missing
            print(f"[MongoStore] renew_lease: atomic renewal DENIED (not holder) for {miner_key}/{install_id}")
            current = self._installations.find_one({"miner_key": miner_key, "install_id": install_id})
            if current and current.get("lease_expires_at"):
                rec = LeaseRecord(
                    miner_key=miner_key,
                    holder_install_id=current.get("lease_install_id", "unknown"),
                    expires_at=current.get("lease_expires_at"),
                    last_seen_at=current.get("last_seen_at", now)
                )
                return False, rec
            return False, None

        except Exception as e:
            # Fallback non-atomic path
            print(f"[MongoStore] renew_lease: FALLBACK to non-atomic (error: {e}) for {miner_key}/{install_id}")
            current = self._installations.find_one({"miner_key": miner_key, "install_id": install_id})
            if not current or current.get("lease_install_id") != install_id:
                rec = None
                if current and current.get("lease_expires_at"):
                    rec = LeaseRecord(
                        miner_key=miner_key,
                        holder_install_id=current.get("lease_install_id", "unknown"),
                        expires_at=current.get("lease_expires_at"),
                        last_seen_at=current.get("last_seen_at", now)
                    )
                return False, rec
            self._installations.update_one(
                {"miner_key": miner_key, "install_id": install_id},
                {"$set": update_set}
            )
            return True, LeaseRecord(miner_key=miner_key, holder_install_id=install_id, expires_at=new_expiry, last_seen_at=now, external_ip=external_ip)

    def lease_status(self, miner_key: str) -> Dict[str, Any]:
        # find any installation doc for this miner with a lease that hasn't expired
        now = datetime.now(timezone.utc)
        rec = self._installations.find_one({"miner_key": miner_key, "lease_expires_at": {"$gt": now}})
        if rec:
            # Active lease found
            expires_at = rec.get("lease_expires_at")
            expires_dt = self._normalize_datetime(expires_at)
            ttl = int((expires_dt - now).total_seconds()) if expires_dt else 0
            expires_iso = expires_dt.isoformat() if isinstance(expires_dt, datetime) else None
            return {"active": True, "holder_install_id": rec.get("lease_install_id"), "expires_at": expires_iso, "ttl_seconds": ttl}
        
        # No active lease found - check for the most recent expired lease
        cursor = self._installations.find(
            {"miner_key": miner_key, "lease_expires_at": {"$exists": True}}
        ).sort("lease_expires_at", -1).limit(1)
        
        rec = next(cursor, None)
        if rec:
            # Return info about the expired lease
            expires_at = rec.get("lease_expires_at")
            expires_dt = self._normalize_datetime(expires_at)
            ttl = int((expires_dt - now).total_seconds()) if expires_dt else 0
            expires_iso = expires_dt.isoformat() if isinstance(expires_dt, datetime) else None
            return {"active": False, "holder_install_id": rec.get("lease_install_id"), "expires_at": expires_iso, "ttl_seconds": ttl}
        
        # No lease found at all
        return {"active": False, "holder_install_id": None, "expires_at": None, "ttl_seconds": 0}

    def _normalize_datetime(self, dt_value) -> Optional[datetime]:
        """Normalize datetime value to UTC datetime object."""
        if isinstance(dt_value, str):
            try:
                return datetime.fromisoformat(dt_value.replace('Z', '+00:00'))
            except Exception:
                return None
        elif isinstance(dt_value, datetime):
            dt = dt_value
        else:
            return None
        
        # If naive, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    # Hardware aggregates
    def get_hardware_doc(self, miner_key: str) -> Dict[str, Any]:
        # Read from PoC.hardware (runtime data with mac, software, PoC, PoL fields).
        # This is the collection that write_status updates via replace_one.
        doc = None
        try:
            doc = self._hardware_docs.find_one({"miner_key": miner_key}) or self._hardware_docs.find_one({"minerKey": miner_key})
        except Exception:
            doc = None
        if not doc:
            return {}
        doc = dict(doc)
        doc.pop("_id", None)
        return doc

    def put_hardware_doc(self, miner_key: str, document: Dict[str, Any]) -> None:
        doc = dict(document)
        doc["miner_key"] = miner_key
        self._hardware_docs.update_one({"miner_key": miner_key}, {"$set": doc}, upsert=True)

    # Presearch
    def put_presearch(self, ip: str, document: Dict[str, Any]) -> None:
        incoming_nodes = document.get("nodes", [])
        # Use $set for top-level fields, then merge nodes by node_key
        top_level = {k: v for k, v in document.items() if k != "nodes"}
        top_level["ip"] = ip

        existing = self._presearch.find_one({"ip": ip})
        if existing and "nodes" in existing:
            merged = {n["node_key"]: n for n in existing["nodes"]}
            for node in incoming_nodes:
                merged[node["node_key"]] = node
            top_level["nodes"] = list(merged.values())
        else:
            top_level["nodes"] = incoming_nodes

        self._presearch.update_one({"ip": ip}, {"$set": top_level}, upsert=True)

    # Measurements
    def upload_measurement(self, hex_id: str, miner_code: str, install_id: str, timestamp: str, measurement_type: str, value: Dict[str, Any]) -> None:
        """Store measurement using daily aggregates in country-specific collection.
        
        Database: measurements
        Collection: determined by hex_id → country mapping (France, Germany, etc.)
        
        Each hex document contains daily aggregates by measurement type:
        {
          "hex_id": "...",
          "country": "France",
          "Bandwidth": {
            "2026-02-03": {"count": 150, "dl": {"min": 0, "max": 5.2, "avg": 1.3}, ...}
          },
          "Satellite": { ... },
          ...
        }
        
        This reduces storage by 99%+ compared to individual records.
        """
        # Use aggregator if available
        if HAS_AGGREGATOR and get_aggregator:
            # First, determine the country to know which collection to use
            aggregator = get_aggregator()
            country = aggregator.get_country_from_hex(hex_id)
            
            # Get the appropriate collection
            measurements_collection = self._get_measurements_collection(country)
            
            # Get existing document
            existing_doc = measurements_collection.find_one({"hex_id": hex_id})
            if existing_doc:
                existing_doc.pop("_id", None)
            
            # Process through aggregator
            updated_doc = aggregator.process_measurement_upload(
                hex_id=hex_id,
                timestamp=timestamp,
                measurement_type=measurement_type,
                value=value,
                existing_doc=existing_doc
            )
            
            # Ensure country is set
            updated_doc['country'] = country
            
            # Replace entire document in country-specific collection
            measurements_collection.update_one(
                {"hex_id": hex_id},
                {"$set": updated_doc},
                upsert=True
            )
        else:
            # Fallback: raw storage in "Unknown" collection
            measurements_collection = self._get_measurements_collection("Unknown")
            
            now = datetime.now(timezone.utc)
            # Parse timestamp to datetime for sorting/filtering
            ts_dt = None
            try:
                ts_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except Exception:
                ts_dt = now
            
            # Prepare the measurement entry
            entry = {
                "timestamp": timestamp,
                "timestamp_dt": ts_dt,
                "miner_code": miner_code,
                "install_id": install_id,
                "value": dict(value),
                "uploaded_at": now,
            }
            
            # Use $push to append to the measurement_type array
            measurements_collection.update_one(
                {"hex_id": hex_id},
                {
                    "$set": {"hex_id": hex_id},
                    "$push": {measurement_type: entry}
                },
                upsert=True
            )

    def get_measurements(self, hex_id: str, start: Optional[datetime], end: Optional[datetime], measurement_types: Optional[List[str]]) -> Dict[str, Any]:
        """Get all measurements for a hex, optionally filtered by date range and measurement types.
        
        Searches across all country collections to find the hex document.
        Returns aggregated data with hex_id and daily statistics by measurement type.
        """
        # Try to find the document across all country collections
        doc = None
        
        # If aggregator is available, try to determine country first
        if HAS_AGGREGATOR and get_aggregator:
            try:
                aggregator = get_aggregator()
                country = aggregator.get_country_from_hex(hex_id)
                measurements_collection = self._get_measurements_collection(country)
                doc = measurements_collection.find_one({"hex_id": hex_id})
            except Exception:
                pass
        
        # If not found or no aggregator, search all collections
        if not doc:
            for collection_name in self._measurements_db.list_collection_names():
                collection = self._measurements_db.get_collection(collection_name)
                doc = collection.find_one({"hex_id": hex_id})
                if doc:
                    break
        
        if not doc:
            return {"hex_id": hex_id}
        
        doc.pop("_id", None)
        result: Dict[str, Any] = {"hex_id": hex_id, "country": doc.get("country", "Unknown")}
        
        # Process each measurement type
        for key, value in doc.items():
            if key == "hex_id":
                continue
            # Skip if measurement_types filter is set and this type isn't in it
            if measurement_types and key not in measurement_types:
                continue
            # Filter array by date range if specified
            if isinstance(value, list):
                filtered = []
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    ts_dt = item.get("timestamp_dt")
                    if start and (not isinstance(ts_dt, datetime) or ts_dt < start):
                        continue
                    if end and (not isinstance(ts_dt, datetime) or ts_dt > end):
                        continue
                    # Remove internal fields for cleaner response
                    clean_item = {k: v for k, v in item.items() if k not in ("timestamp_dt", "uploaded_at")}
                    filtered.append(clean_item)
                if filtered:
                    result[key] = filtered
        
        return result

    # Mysterium keystores
    def upsert_mysterium_keystore(self, miner_key: str, keystore_b64: str, identity_id: Optional[str]) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        payload = {
            "miner_key": miner_key,
            "keystore_b64": keystore_b64,
            "identity_id": identity_id,
            "updated_at": now,
        }
        self._mysterium.update_one(
            {"miner_key": miner_key},
            {"$set": payload},
            upsert=True,
        )
        return dict(payload)

    def get_mysterium_keystore(self, miner_key: str) -> Optional[Dict[str, Any]]:
        doc = self._mysterium.find_one({"miner_key": miner_key})
        if not doc:
            return None
        doc = dict(doc)
        doc.pop("_id", None)
        return doc


# MongoDB store is required - no fallback
def _create_store() -> MongoStore:
    logger = logging.getLogger("ExternalAPI")
    logger.info("Initializing MongoStore (MONGODB_URI required)")
    store = MongoStore()
    
    # Test MongoDB connectivity
    try:
        client = getattr(store, "_client", None)
        if client is not None:
            client.server_info()
        logger.info("Using MongoStore - MongoDB connection successful")
        return store
    except Exception as e:
        raise ConnectionError(f"MongoDB connection failed. Check MONGODB_URI and ensure MongoDB is running. Error: {e}")


STORE = _create_store()
