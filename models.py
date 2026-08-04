from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Union
from typing import List
from enum import Enum

from pydantic import BaseModel, Field


# Determine pydantic major version early so we can set the proper
# Config attribute at class creation time and avoid deprecation warnings.
_pyd_major = 0
try:
    import pydantic as _pyd
    _pyd_ver = getattr(_pyd, "__version__", "0")
    _pyd_major = int(str(_pyd_ver).split(".")[0])
except Exception:
    _pyd_major = 0


# Shared example used in VersionResponse Config
_VERSION_RESPONSE_EXAMPLE = {
    "miner_code": "BM",
    "windows": {
        "software_version_needed": "6.2.0",
        "poc_version_needed": "1.5.0",
    },
    "linux": {
        "software_version_needed": "linux-1.3.0",
        "poc_version_needed": "linux-1.0.0",
    },
    "test-windows": {
        "software_version_needed": "6.3.0",
        "poc_version_needed": "1.6.0",
    },
    "test-linux": {
        "software_version_needed": "linux-1.4.0",
        "poc_version_needed": "linux-1.1.0",
    },
    "multiplier_base": 1.0,
    "multiplier_per_tool": 0.5,
    "limit": 1,
}

_INSTALLER_SUPPORT_EXAMPLE = {
    "os": "windows",
    "miner_codes": ["FEM"]
}


# Examples for other key models
_MINER_PROFILE_EXAMPLE = {
    "miner_key": "miner-key-abc",
    "address": "1abcd...",
    "miner_type": "FEM",
    "credentials": {"mac_address": "AA:BB:CC:DD:EE:FF"},
    "credentials_saved_at": "2025-10-21T06:00:00Z",
    "position": {"lat": 35.0, "lng": -120.0, "hexId": "abc123"},
    "position_saved_at": "2025-10-21T06:05:00Z",
    "verified": True,
}

_VERIFIED_STATUS_EXAMPLE = {
    "miner_key": "miner-key-abc",
    "verified": True,
}


_INSTALLATION_HEARTBEAT_EXAMPLE = {
    "miner_key": "miner-key-abc",
    "install_id": "550e8400-e29b-41d4-a716-446655440000",
    "minerCode": "FEM",
    "software_version_installed": "5.5.7",
    "poc_version_installed": "1.0.0",
    "hostname": "miner-01",
    "os": "Debian 11",
    "last_seen_at": "2025-10-21T07:00:00Z",
    "is_installed": True,
    "software_version_needed": "5.6.0",
    "poc_version_needed": "1.0.0",
    "is_uptodate": False,
    "is_outdated": True,
}


_LEASE_RESPONSE_EXAMPLE = {
    "granted": True,
    "expires_at": "2025-10-21T07:15:00Z",
    "holder_install_id": "550e8400-e29b-41d4-a716-446655440000",
    "ttl_seconds": 900,
}


_LEASE_ACTION_EXAMPLE = {"lease_seconds": 900, "mode": "acquire", "external_ip": "203.0.113.5"}


_HARDWARE_DOCUMENT_EXAMPLE = {"document": {"mac": "AA:BB:CC:DD:EE:FF", "serial": "SN123", "software": "1.2.3"}}


_HARDWARE_RESPONSE_EXAMPLE = {"document": {"mac": "AA:BB:CC:DD:EE:FF", "serial": "SN123", "software": "1.2.3"}}


_MEASUREMENT_UPLOAD_EXAMPLE = {
    "miner_code": "ISM",
    "install_id": "550e8400-e29b-41d4-a716-446655440000",
    "timestamp": "2025-11-10T12:34:56Z",
    "measurement_type": "satellite",
    "value": {
        "satellites_visible": 12,
        "signal_strength": -72
    }
}

_MEASUREMENT_RECORD_EXAMPLE = {
    "hex_id": "8c2a1072b18cdff",
    "bandwidth": [
        {
            "timestamp": "2025-11-10T12:00:00Z",
            "miner_code": "BM",
            "install_id": "550e8400-e29b-41d4-a716-446655440000",
            "value": {"download_mbps": 150.5, "upload_mbps": 20.3}
        }
    ],
    "satellite": [
        {
            "timestamp": "2025-11-10T12:30:00Z",
            "miner_code": "ISM",
            "install_id": "660e8400-e29b-41d4-a716-446655440001",
            "value": {"satellites_visible": 12, "signal_strength": -72}
        }
    ]
}

_MEASUREMENT_LIST_EXAMPLE = {
    "items": [_MEASUREMENT_RECORD_EXAMPLE]
}

_MYSTERIUM_KEYSTORE_REQUEST_EXAMPLE = {
    "miner_key": "REDACTED_ROTATE_ME",
    "keystore_b64": "REDACTED_ROTATE_ME",
    "identity_id": "0xidentity",
}

_MYSTERIUM_KEYSTORE_RESPONSE_EXAMPLE = {
    "keystore_b64": "REDACTED_ROTATE_ME",
    "identity_id": "0xidentity",
}


class VersionPlatformDetails(BaseModel):
    """Version requirements for a specific platform or cohort."""
    software_version_needed: Optional[str] = Field(
        default=None,
        description="Required software version for this platform/cohort (e.g., '6.2.0').",
    )
    poc_version_needed: Optional[str] = Field(
        default=None,
        description="Required PoC version for this platform/cohort (e.g., '1.5.0').",
    )

    class Config:
        pass


class VersionResponse(BaseModel):
    """Response for the versions endpoint using platform-scoped fields."""
    id: Optional[str] = Field(
        default=None,
        alias="_id",
        description="Optional document identifier from the versions collection.",
    )
    miner_code: Optional[str] = Field(
        default=None,
        description="Miner family code (e.g., 'BM').",
    )
    detail: Optional[str] = Field(
        default=None,
        description="Optional human-readable note when platform-specific data is missing.",
    )
    windows: Optional[VersionPlatformDetails] = Field(
        default=None,
        description="Required versions for Windows miners.",
    )
    linux: Optional[VersionPlatformDetails] = Field(
        default=None,
        description="Required versions for Linux miners.",
    )
    test_windows: Optional[VersionPlatformDetails] = Field(
        default=None,
        alias="test-windows",
        description="Required versions for Windows test cohort.",
    )
    test_linux: Optional[VersionPlatformDetails] = Field(
        default=None,
        alias="test-linux",
        description="Required versions for Linux test cohort.",
    )
    multiplier_base: Optional[float] = Field(
        default=None,
        description="Base multiplier for BM rewards.",
    )
    multiplier_per_tool: Optional[float] = Field(
        default=None,
        description="Per-tool multiplier for BM rewards.",
    )
    base_reward: Optional[float] = Field(
        default=None,
        description="Base reward amount for the miner type.",
    )
    limit: Optional[Union[int, str]] = Field(
        default=None,
        description="Installation limit per IP. Numeric (1, 2, 5, 10, etc.) for specific limits, or 'no' for unlimited.",
    )
    reward_amount: Optional[float] = Field(
        default=None,
        description="Reward amount for the miner type.",
    )
    reward_token_asa_id: Optional[str] = Field(
        default=None,
        description="ASA ID of the reward token.",
    )
    reward_token_name: Optional[str] = Field(
        default=None,
        description="Name of the reward token.",
    )
    stake_tiers: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Stake tier configuration with multiplier and label per tier.",
    )


    class Config:
        pass


# Attach the proper schema-example to the nested Config class to avoid
# Pydantic v2 deprecation warnings while keeping static typing happy.
if _pyd_major >= 2:
    setattr(VersionPlatformDetails.Config, "populate_by_name", True)
    setattr(VersionResponse.Config, "populate_by_name", True)
    setattr(VersionResponse.Config, "json_schema_extra", {"example": _VERSION_RESPONSE_EXAMPLE})
else:
    setattr(VersionPlatformDetails.Config, "allow_population_by_field_name", True)
    setattr(VersionResponse.Config, "allow_population_by_field_name", True)
    setattr(VersionResponse.Config, "schema_extra", {"example": _VERSION_RESPONSE_EXAMPLE})


class InstallerSupportResponse(BaseModel):
    os: str = Field(..., description="Normalized operating system identifier (e.g., 'linux', 'windows')")
    miner_codes: List[str] = Field(..., description="Miner codes that have installers for the given OS")

    class Config:
        pass


if _pyd_major >= 2:
    setattr(InstallerSupportResponse.Config, "json_schema_extra", {"example": _INSTALLER_SUPPORT_EXAMPLE})
else:
    setattr(InstallerSupportResponse.Config, "schema_extra", {"example": _INSTALLER_SUPPORT_EXAMPLE})


class MinerCode(str, Enum):
    BM = "BM"
    IDM = "IDM"
    ODM = "ODM"
    ISM = "ISM"
    OSM = "OSM"
    RDN = "RDN"
    SDN = "SDN"
    SVN = "SVN"
    IRM = "IRM"
    FEM = "FEM"


class MinerCodeOrAll(str, Enum):
    """Miner code enum that includes ALL for bulk operations."""
    ALL = "ALL"
    BM = "BM"
    IDM = "IDM"
    ODM = "ODM"
    ISM = "ISM"
    OSM = "OSM"
    RDN = "RDN"
    SDN = "SDN"
    SVN = "SVN"
    IRM = "IRM"
    FEM = "FEM"



class MinerProfileResponse(BaseModel):
    # Full device record fields
    miner_key: Optional[str] = Field(..., description="Full miner key")
    address: Optional[str] = Field(default=None, description="1stParty address string")
    miner_type: Optional[str] = Field(default=None, description="Miner family code (e.g. 'BM', 'node')")
    credentials: Optional[Dict[str, Any]] = Field(default=None, description="Credentials blob, e.g. {'mac_address': '...'}")
    credentials_saved_at: Optional[datetime] = Field(default=None, description="When credentials were saved")
    position: Optional[Dict[str, Any]] = Field(default=None, description="Position object with lat/lng/hexId")
    position_saved_at: Optional[datetime] = Field(default=None, description="When position was recorded")
    verified: bool = Field(default=False, description="Whether the miner has been verified")

    class Config:
        # Allow other fields so we can return the full document shape without failing validation
        extra = "allow"


class VerifiedStatusResponse(BaseModel):
    """Response for the verified status endpoint."""
    miner_key: str = Field(..., description="Full miner key")
    verified: bool = Field(default=False, description="Whether the miner has been verified")
    staked: Optional[Dict[str, Any]] = Field(default=None, description="Staking details if device has an active verification stake.")

    class Config:
        pass


class InstallationHeartbeat(BaseModel):
    """Payload sent by a miner installation to report heartbeat and status.

    Fields are intentionally permissive to allow forward-compatible additions
    from miner binaries.
    """
    miner_key: str = Field(..., description="Full miner key for this device")
    install_id: str = Field(..., description="Unique install instance id (UUID)")
    minerCode: Optional[MinerCode] = Field(default=None, description="Miner family code (optional)")
    software_version_installed: Optional[str] = Field(default=None, description="Current installed software/GUI version")
    poc_version_installed: Optional[str] = Field(default=None, description="Current installed PoC version")
    hostname: Optional[str] = Field(default=None, description="Hostname reported by the miner")
    os: Optional[str] = Field(default=None, description="Operating system string reported by the miner")
    last_seen_at: Optional[datetime] = Field(default=None, description="ISO timestamp when heartbeat was sent")
    is_installed: Optional[bool] = Field(default=None, description="Whether the miner reports the software as installed")
    device_name: Optional[str] = Field(default=None, description="Device display name (e.g., animal name from FEM client)")
    software_version_needed: Optional[str] = Field(default=None, description="Software version required (from versions endpoint)")
    poc_version_needed: Optional[str] = Field(default=None, description="PoC version required (from versions endpoint)")
    is_uptodate: Optional[bool] = Field(default=None, description="Whether the miner considers itself up-to-date")
    is_outdated: Optional[bool] = Field(default=None, description="Whether the miner considers itself outdated")
    class Config:
        extra = "allow"
        pass


class LeaseAction(BaseModel):
    lease_seconds: int = Field(default=900, ge=1, description="Lease duration in seconds")
    external_ip: Optional[str] = Field(default=None, description="Optional external IP address (for BM one-per-IP enforcement)")
    mode: Optional[str] = Field(default=None, description="Optional action mode (e.g., 'acquire' or 'renew')")

    class Config:
        extra = "allow"


class LeaseResponse(BaseModel):
    granted: bool = Field(..., description="Whether the lease was granted to the caller")
    expires_at: Optional[datetime] = Field(default=None, description="ISO expiry timestamp for the lease")
    holder_install_id: Optional[str] = Field(default=None, description="Install id currently holding the lease")
    ttl_seconds: Optional[int] = Field(default=None, description="Time-to-live in seconds for the active lease")
    error_code: Optional[str] = Field(default=None, description="Error code when lease is denied (e.g. 'IP_ALREADY_REGISTERED')")
    class Config:
        pass


class HardwareDocument(BaseModel):
    """Generic hardware document payload. The document shape is flexible and
    may contain PoC or runtime metrics fields (mac, software, PoC/PoL fields, etc.).
    """
    document: Dict[str, Any] = Field(..., description="Arbitrary hardware document")
    class Config:
        pass


class HardwareResponse(BaseModel):
    document: Dict[str, Any] = Field(..., description="Stored hardware document returned from PoC database")
    class Config:
        pass


class MysteriumKeystoreRequest(BaseModel):
    miner_key: str = Field(..., description="Full miner key to associate with this keystore")
    keystore_b64: str = Field(..., description="Base64-encoded contents of keystore.json")
    identity_id: Optional[str] = Field(default=None, description="Optional Mysterium identity id")

    class Config:
        pass


class MysteriumKeystoreResponse(BaseModel):
    keystore_b64: str = Field(..., description="Stored base64-encoded keystore.json contents")
    identity_id: Optional[str] = Field(default=None, description="Optional Mysterium identity id")

    class Config:
        pass


# Attach examples to other models' Configs in a Pydantic-version-aware way
_MODEL_EXAMPLES = [
    (LeaseAction, _LEASE_ACTION_EXAMPLE),
    (LeaseResponse, _LEASE_RESPONSE_EXAMPLE),
    (HardwareDocument, _HARDWARE_DOCUMENT_EXAMPLE),
    (HardwareResponse, _HARDWARE_RESPONSE_EXAMPLE),
    (MysteriumKeystoreRequest, _MYSTERIUM_KEYSTORE_REQUEST_EXAMPLE),
    (MysteriumKeystoreResponse, _MYSTERIUM_KEYSTORE_RESPONSE_EXAMPLE),
    (InstallationHeartbeat, _INSTALLATION_HEARTBEAT_EXAMPLE),
    (MinerProfileResponse, _MINER_PROFILE_EXAMPLE),
    (VerifiedStatusResponse, _VERIFIED_STATUS_EXAMPLE),
]

for _model, _example in _MODEL_EXAMPLES:
    if _pyd_major >= 2:
        setattr(_model.Config, "json_schema_extra", {"example": _example})
    else:
        setattr(_model.Config, "schema_extra", {"example": _example})


class GenericOk(BaseModel):
    ok: bool = True


class ExistsResponse(BaseModel):
    """Response indicating whether a miner_key exists in creds.hardware."""
    exists: bool = Field(..., description="True if the miner_key exists in creds.hardware, False otherwise")

    class Config:
        pass


class RegistrationResponse(BaseModel):
    status: str
    device_token: Optional[str] = None


class RewardsParamsRequest(BaseModel):
    """Request to set BM rewards multiplier parameters."""
    multiplier_base: float = Field(..., description="Base multiplier for BM rewards")
    multiplier_per_tool: float = Field(..., description="Per-tool multiplier for BM rewards")

    class Config:
        pass


class RewardsParamsResponse(BaseModel):
    """Response after updating BM rewards multiplier parameters."""
    miner_code: str = Field(..., description="Miner code the params were set on")
    multiplier_base: float = Field(..., description="Base multiplier for BM rewards")
    multiplier_per_tool: float = Field(..., description="Per-tool multiplier for BM rewards")

    class Config:
        pass


_REWARDS_PARAMS_REQUEST_EXAMPLE = {
    "multiplier_base": 1.0,
    "multiplier_per_tool": 0.5,
}
_REWARDS_PARAMS_RESPONSE_EXAMPLE = {
    "miner_code": "BM",
    "multiplier_base": 1.0,
    "multiplier_per_tool": 0.5,
}
if _pyd_major >= 2:
    setattr(RewardsParamsRequest.Config, "json_schema_extra", {"example": _REWARDS_PARAMS_REQUEST_EXAMPLE})
    setattr(RewardsParamsResponse.Config, "json_schema_extra", {"example": _REWARDS_PARAMS_RESPONSE_EXAMPLE})
else:
    setattr(RewardsParamsRequest.Config, "schema_extra", {"example": _REWARDS_PARAMS_REQUEST_EXAMPLE})
    setattr(RewardsParamsResponse.Config, "schema_extra", {"example": _REWARDS_PARAMS_RESPONSE_EXAMPLE})


class UpdateVersionRequest(BaseModel):
    """Request to update version information for one or more platforms."""
    windows: Optional[VersionPlatformDetails] = Field(
        default=None,
        description="Version requirements for Windows miners.",
    )
    linux: Optional[VersionPlatformDetails] = Field(
        default=None,
        description="Version requirements for Linux miners.",
    )
    test_windows: Optional[VersionPlatformDetails] = Field(
        default=None,
        alias="test-windows",
        description="Version requirements for Windows test cohort.",
    )
    test_linux: Optional[VersionPlatformDetails] = Field(
        default=None,
        alias="test-linux",
        description="Version requirements for Linux test cohort.",
    )
    limit: Optional[Union[int, str]] = Field(
        default=None,
        description="Installation limit per IP. Numeric (1, 2, 5, 10, etc.) for specific limits, or 'no' for unlimited.",
    )
    reward_amount: Optional[float] = Field(
        default=None,
        description="Reward amount for the miner type.",
    )
    reward_token_asa_id: Optional[str] = Field(
        default=None,
        description="ASA ID of the reward token.",
    )
    reward_token_name: Optional[str] = Field(
        default=None,
        description="Name of the reward token.",
    )


    class Config:
        pass


class MeasurementUpload(BaseModel):
    """Measurement data upload from a miner instance."""
    miner_code: MinerCode = Field(..., description="Miner type code (BM, ISM, IRM, etc.)")
    install_id: str = Field(..., description="Installation UUID to distinguish multiple miners")
    timestamp: str = Field(..., description="ISO 8601 timestamp of the measurement")
    measurement_type: str = Field(..., description="Type of measurement (e.g., 'bandwidth', 'satellite', 'decibel', 'radiation')")
    value: Dict[str, Any] = Field(..., description="Measurement value (flexible schema)")
    
    class Config:
        extra = "allow"
        pass


class MeasurementRecord(BaseModel):
    """Aggregated measurement data for a hex, organized by measurement type."""
    hex_id: str = Field(..., description="H3 hex cell ID (registered location)")
    # Dynamic fields based on measurement types available
    class Config:
        extra = "allow"
        pass


class MeasurementListResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(..., description="List of hex measurement records")
    class Config:
        pass


class PresearchNode(BaseModel):
    miner_key: str = Field(..., description="Miner key")
    node_key: str = Field(..., description="Public key of the node")
    connected: bool = Field(..., description="Whether the node is connected")
    blocked: bool = Field(..., description="Whether the node is blocked")
    description: Optional[str] = Field(None, description="Node description from meta")
    node_name: Optional[str] = Field(None, description="Truncated miner-key suffix used as container/node label")


class ProductRewardResponse(BaseModel):
    key: str = Field(..., description="Product/miner code key (e.g., FEM)")
    reward_amount: Optional[float] = Field(None, description="Token-denominated daily base reward amount")
    reward_token_asa_id: Optional[str] = Field(None, description="ASA ID of the reward token")
    reward_token_name: Optional[str] = Field(None, description="Display name of the reward token")
    stake_token_asa_id: Optional[str] = Field(None, description="ASA ID of the stake token")
    stake_token_name: Optional[str] = Field(None, description="Display name of the stake token")

    class Config:
        pass


class PresearchPayload(BaseModel):
    ip: str = Field(..., description="Remote address")
    timestamp: str = Field(..., description="ISO8601 UTC timestamp")
    nodes: List[PresearchNode] = Field(..., description="List of Presearch nodes")




class MigrationRequest(BaseModel):
    """Payload for POST /devices/migrate -- deactivate old keys on FEM migration."""
    fem_key: str = Field(..., description='New FEM miner key (must match ^FEM-[a-fA-F0-9]{32}$)')
    old_keys: List[str] = Field(..., description='Old miner keys to deactivate (max 20)')

# Attach example for ExistsResponse
if _pyd_major >= 2:
    setattr(ExistsResponse.Config, "json_schema_extra", {"example": {"exists": True}})
else:
    setattr(ExistsResponse.Config, "schema_extra", {"example": {"exists": True}})

# Attach example for UpdateVersionRequest
_UPDATE_VERSION_EXAMPLE = {
    "windows": {"software_version_needed": "6.2.0", "poc_version_needed": "1.5.0"},
    "linux": {"software_version_needed": "linux-1.3.0"},
    "test-windows": {"software_version_needed": "6.3.0", "poc_version_needed": "1.6.0"},
    "limit": 1,
}
if _pyd_major >= 2:
    setattr(UpdateVersionRequest.Config, "populate_by_name", True)
    setattr(UpdateVersionRequest.Config, "json_schema_extra", {"example": _UPDATE_VERSION_EXAMPLE})
else:
    setattr(UpdateVersionRequest.Config, "allow_population_by_field_name", True)
    setattr(UpdateVersionRequest.Config, "schema_extra", {"example": _UPDATE_VERSION_EXAMPLE})

# Attach example for MeasurementUpload
if _pyd_major >= 2:
    setattr(MeasurementUpload.Config, "json_schema_extra", {"example": _MEASUREMENT_UPLOAD_EXAMPLE})
else:
    setattr(MeasurementUpload.Config, "schema_extra", {"example": _MEASUREMENT_UPLOAD_EXAMPLE})

# Attach examples for measurement response models
if _pyd_major >= 2:
    setattr(MeasurementRecord.Config, "json_schema_extra", {"example": _MEASUREMENT_RECORD_EXAMPLE})
    setattr(MeasurementListResponse.Config, "json_schema_extra", {"example": _MEASUREMENT_LIST_EXAMPLE})
else:
    setattr(MeasurementRecord.Config, "schema_extra", {"example": _MEASUREMENT_RECORD_EXAMPLE})
    setattr(MeasurementListResponse.Config, "schema_extra", {"example": _MEASUREMENT_LIST_EXAMPLE})
