from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import importlib.util
import logging
import os
from pathlib import Path as PathLib
import sys
import re
import ipaddress

# Try to load .env automatically when possible (non-fatal)
dotenv_spec = importlib.util.find_spec("dotenv")
if dotenv_spec is not None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        # don't block startup if dotenv cannot be loaded
        pass

# Resolve 1Password references in environment values.
# Supported formats in .env:
#   op/Vault/Item/field
#   op://Vault/Item/field
# These are translated to the 1Password CLI URI format and fetched using `op read`.
import os
import subprocess
import shlex


def _fetch_op_secret(ref: str) -> str:
    """Fetch a value from 1Password CLI. Returns the raw ref on failure.

    Expects ref like 'op/Vault/Item/field' or 'op://Vault/Item/field'.
    """
    if not ref:
        return ref
    norm = ref
    if ref.startswith("op://"):
        norm = ref
    elif ref.startswith("op/"):
        # convert op/Vault/Item/field -> op://Vault/Item/field
        norm = "op://" + ref[len("op/"):]
    else:
        return ref

    try:
        # call `op read 'op://vault/item/field'`
        # ensure we don't invoke a shell; pass the URI as a single arg
        res = subprocess.run(["op", "read", norm], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except FileNotFoundError:
        # op CLI not available
        print(f"[ExternalAPI] 1Password CLI 'op' not found; leaving env var as-is")
        return ref
    except subprocess.CalledProcessError as e:
        print(f"[ExternalAPI] Failed to read 1Password secret {norm}: {e}; leaving env var as-is")
        return ref


# Resolve any env vars that look like op references (non-fatal)
for k, v in list(os.environ.items()):
    if isinstance(v, str) and (v.startswith("op/") or v.startswith("op://")):
        os.environ[k] = _fetch_op_secret(v)

from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from contextlib import asynccontextmanager
from fastapi.openapi.utils import get_openapi
from fastapi.openapi.docs import get_swagger_ui_html
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from models import (
    ExistsResponse,
    GenericOk,
    HardwareDocument,
    HardwareResponse,
    InstallationHeartbeat,
    LeaseAction,
    LeaseResponse,
    MinerProfileResponse,
    VersionResponse,
    InstallerSupportResponse,
    MinerCode,
    MinerCodeOrAll,
    UpdateVersionRequest,
    MeasurementUpload,
    MeasurementListResponse,
    MeasurementRecord,
    MysteriumKeystoreRequest,
    MysteriumKeystoreResponse,
    PresearchPayload,
    RewardsParamsRequest,
    RewardsParamsResponse,
    VerifiedStatusResponse,
)
from storage import STORE

# Configure logging with timestamps and file output
def setup_logging():
    """Set up logging with both console and file output with timestamps."""
    # Import colorama for colored console output
    try:
        from colorama import init, Fore, Style
        init(autoreset=True)  # Automatically reset colors after each print
        colors_available = True
        
        # Add custom log level for API access errors
        API_ACCESS_ERROR_LEVEL = 35  # Between WARNING (30) and ERROR (40)
        logging.addLevelName(API_ACCESS_ERROR_LEVEL, "API ACCESS ERROR")
        
        color_codes = {
            'DEBUG': Fore.CYAN,
            'INFO': Fore.GREEN,
            'WARNING': Fore.YELLOW,
            'ERROR': Fore.RED + Style.BRIGHT,
            'CRITICAL': Fore.RED + Style.BRIGHT,
            'API ACCESS ERROR': Fore.RED + Style.BRIGHT,
            # Short auth notes (used in middleware) should be bright red
            'MISSING TOKEN': Fore.RED + Style.BRIGHT,
            'INVALID TOKEN': Fore.RED + Style.BRIGHT,
        }
        reset_code = Style.RESET_ALL
    except ImportError:
        colors_available = False
        color_codes = {}
        reset_code = ""
    
    # Create logs directory if it doesn't exist
    log_dir = PathLib("logs")
    log_dir.mkdir(exist_ok=True)
    
    # Create log filename with current date
    log_filename = log_dir / f"hardware_api_{datetime.now().strftime('%Y%m%d')}.log"
    
    # Create custom colored formatter for console
    class ColoredFormatter(logging.Formatter):
        """Custom formatter that adds colors to console output with HTTP status code coloring."""
        
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.colors = color_codes
            self.reset = reset_code
            # HTTP status code colors - use the outer scope variables
            if colors_available:
                from colorama import Fore, Style
                self.status_colors = {
                    '200': Fore.GREEN,
                    '201': Fore.GREEN,
                    '202': Fore.GREEN,
                    '204': Fore.GREEN,
                    '301': Fore.YELLOW,
                    '302': Fore.YELLOW,
                    '307': Fore.YELLOW,
                    '308': Fore.YELLOW,
                    '400': Fore.RED + Style.BRIGHT,
                    '401': Fore.RED + Style.BRIGHT,
                    '403': Fore.RED + Style.BRIGHT,
                    '404': Fore.RED + Style.BRIGHT,
                    '422': Fore.RED + Style.BRIGHT,
                    '429': Fore.RED + Style.BRIGHT,
                    '500': Fore.RED + Style.BRIGHT,
                    '502': Fore.RED + Style.BRIGHT,
                    '503': Fore.RED + Style.BRIGHT,
                }
            else:
                self.status_colors = {}
        
        def format(self, record):
            if colors_available:
                # Add color to the level name
                # Our middleware builds messages like: "<IP> - [LEVEL] rest..."
                # Color the bracketed level label inside the message for clarity.
                formatted = super().format(record)

                # Color the bracketed level label (e.g., [INFO], [API ACCESS ERROR])
                level_label_pattern = r"\[([A-Z ]+)\]"

                def color_label(m):
                    label = m.group(1)
                    color = self.colors.get(label, None)
                    if color:
                        return f"{color}[{label}]{self.reset}"
                    return m.group(0)

                formatted = re.sub(level_label_pattern, color_label, formatted)

                # Color only the HTTP status code that appears immediately after
                # the bracketed level label (pattern: "] - <STATUS>"). This avoids
                # accidentally coloring IP octets (e.g. "204" in "204.76.203.219").
                status_pattern = r'(?<=\] - )([1-5][0-9]{2})\b'

                def color_status(match):
                    status = match.group(1)
                    if status in self.status_colors:
                        return f"{self.status_colors[status]}{status}{self.reset}"
                    return status

                formatted = re.sub(status_pattern, color_status, formatted)

                return formatted
            
            return super().format(record)
    
    # Configure logging format with timestamp. Message will include IP and level label.
    log_format = '[%(asctime)s] - %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'
    
    # Create handlers
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredFormatter(log_format, datefmt=date_format))
    
    file_handler = logging.FileHandler(log_filename, mode='a', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    
    # Set up logging configuration
    logging.basicConfig(
        level=logging.INFO,
        handlers=[console_handler, file_handler]
    )
    
    # Configure uvicorn loggers to use our colored formatter but disable access logging
    # We'll handle access logging in our middleware instead
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.handlers = [console_handler, file_handler]
    uvicorn_logger.propagate = False

    # Disable uvicorn access logger to avoid duplicate logs
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.handlers = []
    uvicorn_access_logger.propagate = False

    # Create two specialized loggers: console-only and file-only so we can
    # selectively include sensitive fragments (like full User-Agent) in file logs
    console_logger = logging.getLogger("ExternalAPI.console")
    console_logger.handlers = [console_handler]
    console_logger.propagate = False
    console_logger.setLevel(logging.INFO)

    file_logger = logging.getLogger("ExternalAPI.file")
    file_logger.handlers = [file_handler]
    file_logger.propagate = False
    file_logger.setLevel(logging.INFO)

    # Also provide a general app logger (keeps previous behavior for other messages)
    logger = logging.getLogger("ExternalAPI")
    logger.handlers = [console_handler, file_handler]
    logger.propagate = False

    logger.info("Logging initialized - console and file output enabled with colors")
    return logger

# Initialize logging
logger = setup_logging()

# Initialize rate limiter
# Rate limit can be configured via FLXTIME_RATE_LIMIT env var (default: 100 requests per minute)
limiter = Limiter(key_func=get_remote_address, default_limits=[])

# Bearer token security schemes for protected endpoints
bearer_scheme = HTTPBearer(auto_error=False)


def verify_bearer_token_flxtime(request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> str:
    """Validate bearer token against API_BEARER_TOKEN_FLXTIME or API_BEARER_TOKEN environment variables.
    
    Used for FlxTime-specific endpoints like /credentials/{miner_key}/exists.
    Accepts either the FlxTime-specific token OR the general API token.
    Raises HTTPException 401 if token is missing or invalid.
    Returns the token if valid.
    """
    flxtime_token = os.getenv("API_BEARER_TOKEN_FLXTIME")
    general_token = os.getenv("API_BEARER_TOKEN")
    
    if not flxtime_token and not general_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_BEARER_TOKEN_FLXTIME or API_BEARER_TOKEN not configured on server"
        )
    
    if not credentials:
        # attach a short auth note for middleware logging and raise
        request.state.auth_note = "MISSING TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Accept either the FlxTime-specific token OR the general token
    if credentials.credentials != flxtime_token and credentials.credentials != general_token:
        request.state.auth_note = "INVALID TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # mark success briefly for possible middleware use (optional)
    request.state.auth_note = "OK"
    return credentials.credentials


def verify_bearer_token_general(request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> str:
    """Validate bearer token against API_BEARER_TOKEN environment variable.
    
    Used for general API endpoints (versions, credentials, installations, leases, PoC).
    Raises HTTPException 401 if token is missing or invalid.
    Returns the token if valid.
    """
    expected_token = os.getenv("API_BEARER_TOKEN")
    
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_BEARER_TOKEN not configured on server"
        )
    
    if not credentials:
        request.state.auth_note = "MISSING TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if credentials.credentials != expected_token:
        request.state.auth_note = "INVALID TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.auth_note = "OK"
    return credentials.credentials


def verify_bearer_token_admin(request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> str:
    """Validate bearer token against API_BEARER_TOKEN_ADMIN environment variable.

    This token is intended for protecting admin-only endpoints (unblock/list operations).
    """
    admin_token = os.getenv("API_BEARER_TOKEN_ADMIN")

    if not admin_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_BEARER_TOKEN_ADMIN not configured on server"
        )

    if not credentials:
        request.state.auth_note = "MISSING TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials.credentials != admin_token:
        request.state.auth_note = "INVALID TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.auth_note = "OK"
    return credentials.credentials


def verify_bearer_token_dropwireless(request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> str:
    """Validate bearer token against API_BEARER_TOKEN_DROPWIRELESS environment variable.

    Used for DropWireless partner endpoints (leases only).
    Raises HTTPException 401 if token is missing or invalid.
    Returns the token if valid.
    """
    expected_token = os.getenv("API_BEARER_TOKEN_DROPWIRELESS")
    
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_BEARER_TOKEN_DROPWIRELESS not configured on server"
        )
    
    if not credentials:
        request.state.auth_note = "MISSING TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if credentials.credentials != expected_token:
        request.state.auth_note = "INVALID TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.auth_note = "OK"
    return credentials.credentials


def verify_bearer_token_leases(request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> str:
    """Validate lease access bearer token.

    Accepts either the general API token or the DropWireless-specific token so existing
    clients retain access while DropWireless remains scoped to lease endpoints only.
    """
    general_token = os.getenv("API_BEARER_TOKEN")
    dropwireless_token = os.getenv("API_BEARER_TOKEN_DROPWIRELESS")

    if not general_token and not dropwireless_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_BEARER_TOKEN or API_BEARER_TOKEN_DROPWIRELESS not configured on server"
        )

    if not credentials:
        request.state.auth_note = "MISSING TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    provided = credentials.credentials
    if general_token and provided == general_token:
        request.state.auth_note = "OK"
        return provided
    if dropwireless_token and provided == dropwireless_token:
        request.state.auth_note = "OK"
        return provided

    request.state.auth_note = "INVALID TOKEN"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )


_openapi_full = None
_openapi_admin = None
_openapi_flxtime = None
_openapi_general = None
_openapi_dropwireless = None
_openapi_public = None


def _build_full_openapi(app: FastAPI):
    return get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )


def _rebuild_openapi_caches(app: FastAPI) -> None:
    """(Re)build role-scoped OpenAPI caches from current app.routes.

    Useful when routes change without a full process restart (e.g., reload issues)
    or when forcing a refresh via an admin endpoint.
    """
    try:
        global _openapi_full, _openapi_admin, _openapi_flxtime, _openapi_general, _openapi_dropwireless, _openapi_public
        _openapi_full = _build_full_openapi(app)
        _openapi_admin = _filter_schema_by_roles(
            _openapi_full, include_admin=True, include_flxtime=True, allowed_tags={"Admin", "Health"}
        )
        _openapi_admin["info"]["description"] = "Admin API: Manage scanner bans, IP whitelists, and system health."

        _openapi_flxtime = _filter_schema_by_roles(
            _openapi_full, include_admin=False, include_flxtime=True, allowed_tags={"FlxTime", "Health"}
        )
        _openapi_flxtime["info"]["description"] = "FlxTime API: Check miner existence in FryNetworks hardware database."

        _openapi_general = _filter_schema_by_roles(
            _openapi_full, include_admin=False, include_flxtime=True
        )
        _openapi_general["info"]["description"] = (
            "General API: Access to version management, credentials, installations, leases, measurements, Mysterium keystores, and PoC documents."
        )

        _openapi_dropwireless = _filter_schema_by_roles(
            _openapi_full, include_admin=False, include_flxtime=False, allowed_tags={"Leases", "Health"}
        )
        _openapi_dropwireless["info"]["description"] = "DropWireless API: Access to lease management endpoints."

        _openapi_public = _filter_schema_by_roles(
            _openapi_full, include_admin=False, include_flxtime=False, public_only=True
        )
        _openapi_public["info"]["description"] = "Public API: No endpoints available. Please authenticate to access documentation."
    except Exception:
        # Non-fatal; leave caches as-is
        pass


def _filter_schema_by_roles(
    schema: Dict[str, Any],
    include_admin: bool,
    include_flxtime: bool,
    public_only: bool = False,
    allowed_tags: set[str] | None = None,
) -> Dict[str, Any]:
    import copy
    filtered = copy.deepcopy(schema)
    paths = filtered.get("paths", {})
    new_paths = {}
    used_schemas = set()
    
    for path, ops in paths.items():
        new_ops = {}
        for method, op in ops.items():
            if not isinstance(op, dict):
                new_ops[method] = op
                continue
            tags = op.get("tags", [])
            # Public-only or explicit allow-list: include only allowed tags
            if public_only:
                if "Health" in tags:
                    new_ops[method] = op
                    _collect_schema_refs(op, used_schemas)
            elif allowed_tags is not None:
                if any(t in allowed_tags for t in tags):
                    new_ops[method] = op
                    _collect_schema_refs(op, used_schemas)
            else:
                # Filter admin
                if not include_admin and "Admin" in tags:
                    continue
                # Filter FlxTime
                if not include_flxtime and "FlxTime" in tags:
                    continue
                # Otherwise include
                new_ops[method] = op
                _collect_schema_refs(op, used_schemas)
        if new_ops:
            new_paths[path] = new_ops
    
    filtered["paths"] = new_paths
    
    # Filter components/schemas to only include used ones
    if "components" in filtered and "schemas" in filtered["components"]:
        all_schemas = filtered["components"]["schemas"]
        filtered_schemas = {}
        # Recursively collect all referenced schemas
        to_process = list(used_schemas)
        processed = set()
        while to_process:
            schema_name = to_process.pop()
            if schema_name in processed or schema_name not in all_schemas:
                continue
            processed.add(schema_name)
            filtered_schemas[schema_name] = all_schemas[schema_name]
            # Find references within this schema
            _collect_schema_refs(all_schemas[schema_name], to_process)
        
        filtered["components"]["schemas"] = filtered_schemas
    
    return filtered


def _collect_schema_refs(obj: Any, refs: Any) -> None:
    """Recursively collect $ref schema names from an object."""
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref = obj["$ref"]
            # Extract schema name from "#/components/schemas/SchemaName"
            if ref.startswith("#/components/schemas/"):
                schema_name = ref.split("/")[-1]
                if isinstance(refs, set):
                    refs.add(schema_name)
                elif isinstance(refs, list):
                    refs.append(schema_name)
        for value in obj.values():
            _collect_schema_refs(value, refs)
    elif isinstance(obj, list):
        for item in obj:
            _collect_schema_refs(item, refs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup: augment generated OpenAPI schema with human-readable
    # descriptions for MinerCode enum values and cache the modified schema.
    try:
        from models import MinerCode

        enum_desc = {
            "BM": "Bandwidth Miner",
            "IDM": "Indoor Decibel Miner",
            "ODM": "Outdoor Decibel Miner",
            "ISM": "Indoor Satellite Miner",
            "OSM": "Outdoor Satellite Miner",
            "RDN": "Reward Decentralization Node",
            "SDN": "Storage Decentralization Node",
            "SVN": "Storage Validator Node",
            "AEM": "AI Edge Miner",
            "IRM": "Indoor Radiation Miner",
        }

        # Generate and cache the OpenAPI schema once. Avoid injecting
        # vendor-specific fields such as `x-enum-descriptions` into the
        # parameter schemas to prevent raw lists from showing in the UI.
        # Build and cache a base OpenAPI (will be further filtered per role routes)
        global _openapi_full, _openapi_admin, _openapi_flxtime, _openapi_general, _openapi_dropwireless, _openapi_public
        _openapi_full = _build_full_openapi(app)
        # Derive role-based variants per requirements:
        # - Admin: Admin + Health only
        _openapi_admin = _filter_schema_by_roles(
            _openapi_full, include_admin=True, include_flxtime=True, allowed_tags={"Admin", "Health"}
        )
        _openapi_admin["info"]["description"] = "Admin API: Manage scanner bans, IP whitelists, and system health."
        # - FlxTime: FlxTime + Health only
        _openapi_flxtime = _filter_schema_by_roles(
            _openapi_full, include_admin=False, include_flxtime=True, allowed_tags={"FlxTime", "Health"}
        )
        _openapi_flxtime["info"]["description"] = "FlxTime API: Check miner existence in FryNetworks hardware database."
        # - General: everything except Admin (includes FlxTime and all general tags)
        _openapi_general = _filter_schema_by_roles(
            _openapi_full, include_admin=False, include_flxtime=True
        )
        _openapi_general["info"]["description"] = (
            "General API: Access to version management, credentials, installations, leases, measurements, Mysterium keystores, and PoC documents."
        )
        # - DropWireless: Leases + Health only
        _openapi_dropwireless = _filter_schema_by_roles(
            _openapi_full, include_admin=False, include_flxtime=False, allowed_tags={"Leases", "Health"}
        )
        _openapi_dropwireless["info"]["description"] = "DropWireless API: Access to lease management endpoints."
        # - Public: Health only
        _openapi_public = _filter_schema_by_roles(
            _openapi_full, include_admin=False, include_flxtime=False, public_only=True
        )
        _openapi_public["info"]["description"] = "Public API: No endpoints available. Please authenticate to access documentation."
    except Exception:
        # Non-fatal; continue startup even if we couldn't augment OpenAPI
        pass

    # Load persistent manual bans (if file exists). Expect a list of objects with keys: ip, reason, ts
    try:
        bans_file = PathLib("deployment/banned_ips.json")
        if bans_file.exists():
            import json
            data = json.loads(bans_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        ip = entry.get("ip")
                        if ip:
                            _persistent_bans[ip] = entry
                            _probe_blocklist[ip] = float("inf")
                            logging.getLogger("ExternalAPI.file").warning(f"LOADED_PERSISTENT_BAN ip={ip} reason={entry.get('reason')}")
    except Exception:
        logging.getLogger("ExternalAPI.file").debug("failed to load persistent bans")

    # Load whitelist from env and file
    try:
        import json
        # from env
        env_wl = os.getenv("WHITELIST_IPS", "")
        items = [x.strip() for x in env_wl.split(",") if x.strip()]
        # from file
        wl_file = PathLib("deployment/whitelist_ips.json")
        if wl_file.exists():
            file_items = json.loads(wl_file.read_text(encoding="utf-8")) or []
            if isinstance(file_items, list):
                items.extend([x for x in file_items if isinstance(x, str)])
        # parse items
        for item in items:
            try:
                if "/" in item:
                    net = ipaddress.ip_network(item, strict=False)
                    _ip_whitelist_nets.append(net)
                else:
                    # validate IP and store normalized string
                    ip_str = str(ipaddress.ip_address(item))
                    _ip_whitelist_ips.add(ip_str)
            except Exception:
                logging.getLogger("ExternalAPI.file").debug("invalid whitelist entry: %s", item)
        # Remove any whitelisted IPs from blocklist for cleanliness
        for ip in list(_probe_blocklist.keys()):
            try:
                if _is_whitelisted(ip):
                    _probe_blocklist.pop(ip, None)
                    logging.getLogger("ExternalAPI.file").warning(f"WHITELIST_UNBLOCK_AT_STARTUP ip={ip}")
            except Exception:
                continue
        # Also remove any trusted proxy IPs that got accidentally banned
        for ip in list(_probe_blocklist.keys()):
            if ip in _trusted_proxy_set:
                _probe_blocklist.pop(ip, None)
                _persist_remove_ban(ip)
                logging.getLogger("ExternalAPI.file").warning(
                    "REMOVED_TRUSTED_PROXY_BAN_AT_STARTUP ip=%s", ip
                )
    except Exception:
        logging.getLogger("ExternalAPI.file").debug("failed to load whitelist")

    yield


# Define tags metadata to control the order and descriptions in the documentation
tags_metadata = [
    {
        "name": "FlxTime",
        "description": "Special endpoints for FlxTime partner integration. These endpoints require specific authentication tokens and have dedicated rate limiting.",
    },
    {
        "name": "Health",
        "description": "Service health and status endpoints.",
    },
    {
        "name": "Versions",
        "description": "Hardware miner software version management.",
    },
    {
        "name": "Credentials",
        "description": "Credential and profile lookup from the credentials database.",
    },
    {
        "name": "Installations",
        "description": "Installation heartbeats and tracking.",
    },
    {
        "name": "Leases",
        "description": "Lease coordination and management.",
    },
    {
        "name": "PoC",
        "description": "Proof of Connectivity (PoC) document storage and retrieval.",
    },
    {
        "name": "Mysterium",
        "description": "Store and retrieve Mysterium keystore blobs per miner key.",
    },
    {
        "name": "Measurements",
        "description": "Upload and query measurement data from miners indexed by hex location.",
    },
]

app = FastAPI(
    title="FryNetworks Hardware API",
    version="2.0.0",
    summary="Reference implementation of the FryNetworks hardware miner HTTP contract.",
    description=(
        "This service exposes endpoints for: \n"
        " - Version management (required hardware miner software)\n"
        " - Credential/profile lookup (from creds database)\n"
        " - Installation heartbeats and lease coordination (PoC database)\n"
        " - Hardware/PoC document storage\n\n"
        "Endpoints are grouped by functional area in the UI. Request/response schemas"
        " are documented using Pydantic models"
    ),
    openapi_tags=tags_metadata,
    lifespan=lifespan,
    docs_url=None,  # Disable default docs
    redoc_url=None,  # Disable redoc
    openapi_url=None,  # Disable default openapi.json
)

# Ensure real client IPs are used when behind a trusted proxy (e.g., nginx).
# Configure trusted proxy IPs via TRUSTED_PROXY_IPS env var (comma-separated).
_trusted_proxy_env = os.getenv("TRUSTED_PROXY_IPS", "127.0.0.1,::1").strip()
if _trusted_proxy_env == "*":
    _trusted_proxies: Any = "*"
else:
    _trusted_proxies = [h.strip() for h in _trusted_proxy_env.split(",") if h.strip()]
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=_trusted_proxies)  # type: ignore[arg-type]
# Build a set of trusted proxy IPs for fast lookups in ban logic.
_trusted_proxy_set: set = set()
if isinstance(_trusted_proxies, list):
    _trusted_proxy_set = set(_trusted_proxies)

# Simple in-process tracker for repeated 404 probes and a temporary blocklist.
# This is intended as a lightweight mitigation for opportunistic scanners.
# Configurable via environment variables:
#   PROBE_404_WINDOW (seconds) - sliding window to count 404s (default 60)
#   PROBE_404_THRESHOLD - number of 404s in window to trigger a block (default 20)
#   PROBE_BLOCK_SECONDS - how long to block an offender (default 600)
from collections import defaultdict, deque
import asyncio

_probe_404_window = int(os.getenv("PROBE_404_WINDOW", "60"))
_probe_404_threshold = int(os.getenv("PROBE_404_THRESHOLD", "20"))
_probe_block_seconds = int(os.getenv("PROBE_BLOCK_SECONDS", "600"))
_probe_404_consec_threshold = int(os.getenv("PROBE_404_CONSEC_THRESHOLD", "12"))

# maps ip -> deque[timestamps_of_404s]
_probe_404_counters: Dict[str, deque] = defaultdict(lambda: deque())
# maps ip -> count of consecutive 404s
_probe_404_consec: Dict[str, int] = defaultdict(int)
# maps ip -> blocked_until_timestamp
_probe_blocklist: Dict[str, float] = {}
# lock for concurrency safety
_probe_lock = asyncio.Lock()
_persistent_bans: Dict[str, Dict[str, Any]] = {}

# Whitelist structures
_ip_whitelist_ips: set[str] = set()
_ip_whitelist_nets: list = []

def _is_whitelisted(ip: str) -> bool:
    try:
        # Always whitelist localhost and private IPs
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_loopback or ip_obj.is_private:
            return True
        if ip in _ip_whitelist_ips:
            return True
        for net in _ip_whitelist_nets:
            try:
                if ip_obj in net:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _atomic_write_json(path: PathLib, data: object) -> None:
    """Write JSON to path atomically using a temp file + replace."""
    import json
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # atomic replace
    os.replace(str(tmp), str(path))


def _persist_add_ban(ip: str, reason: str = "") -> None:
    """Add a persistent ban entry with metadata (idempotent).

    Stored format: [{"ip": "1.2.3.4", "reason": "sensitive_probe", "ts": 169...}, ...]
    """
    # Never ban trusted proxy IPs -- they're infrastructure, not clients
    if ip in _trusted_proxy_set:
        logging.getLogger("ExternalAPI.file").warning(
            "SKIPPED_BAN_TRUSTED_PROXY ip=%s reason=%s", ip, reason
        )
        return
    try:
        bans_file = PathLib("deployment/banned_ips.json")
        import json, time
        existing = []
        if bans_file.exists():
            existing = json.loads(bans_file.read_text(encoding="utf-8")) or []
        # Build a map for idempotency
        by_ip = {e.get("ip"): e for e in existing if isinstance(e, dict) and e.get("ip")}
        if ip not in by_ip:
            entry = {"ip": ip, "reason": reason, "ts": int(time.time())}
            existing.append(entry)
            _atomic_write_json(bans_file, existing)
            _persistent_bans[ip] = entry
            logging.getLogger("ExternalAPI.file").warning(f"ADDED_PERSISTENT_BAN ip={ip} reason={reason}")
        else:
            # update in-memory map if missing
            _persistent_bans[ip] = by_ip[ip]
    except Exception:
        logging.getLogger("ExternalAPI.file").exception("failed to persist added ban for %s", ip)


def _persist_remove_ban(ip: str) -> bool:
    """Remove a persistent ban entry by IP if present. Returns True if removed."""
    try:
        bans_file = PathLib("deployment/banned_ips.json")
        import json
        if not bans_file.exists():
            _persistent_bans.pop(ip, None)
            return False
        existing = json.loads(bans_file.read_text(encoding="utf-8")) or []
        new_list = [e for e in existing if not (isinstance(e, dict) and e.get("ip") == ip)]
        if len(new_list) != len(existing):
            _atomic_write_json(bans_file, new_list)
            _persistent_bans.pop(ip, None)
            logging.getLogger("ExternalAPI.file").warning(f"REMOVED_PERSISTENT_BAN ip={ip}")
            return True
        return False
    except Exception:
        logging.getLogger("ExternalAPI.file").exception("failed to persist removed ban for %s", ip)
        return False
# Whitelist persistence helpers
def _whitelist_file() -> PathLib:
    return PathLib("deployment/whitelist_ips.json")


def _persist_whitelist_list() -> list[str]:
    try:
        import json
        wf = _whitelist_file()
        if wf.exists():
            data = json.loads(wf.read_text(encoding="utf-8")) or []
            return [x for x in data if isinstance(x, str)]
    except Exception:
        logging.getLogger("ExternalAPI.file").debug("failed reading whitelist file")
    return []


def _persist_whitelist_write(entries: list[str]) -> None:
    # Atomic write
    _atomic_write_json(_whitelist_file(), entries)


def _normalize_wl_entry(entry: str) -> tuple[str, str]:
    """Return (kind, normalized_string) where kind is 'ip' or 'cidr'. Raises on invalid."""
    entry = entry.strip()
    if "/" in entry:
        net = ipaddress.ip_network(entry, strict=False)
        return ("cidr", str(net))
    else:
        ip_str = str(ipaddress.ip_address(entry))
        return ("ip", ip_str)


def _whitelist_add(entry: str) -> dict:
    kind, norm = _normalize_wl_entry(entry)
    # Update memory
    if kind == "ip":
        _ip_whitelist_ips.add(norm)
        # Also remove any existing bans for this IP
        if norm in _probe_blocklist:
            _probe_blocklist.pop(norm, None)
        _persist_remove_ban(norm)
    else:
        _ip_whitelist_nets.append(ipaddress.ip_network(norm, strict=False))
        # Remove pre-existing bans within this net (best-effort)
        for ip in list(_probe_blocklist.keys()):
            try:
                if ipaddress.ip_address(ip) in _ip_whitelist_nets[-1]:
                    _probe_blocklist.pop(ip, None)
                    _persist_remove_ban(ip)
            except Exception:
                continue
    # Update file list idempotently
    items = _persist_whitelist_list()
    if norm not in items:
        items.append(norm)
        _persist_whitelist_write(items)
    logging.getLogger("ExternalAPI.file").warning(f"WHITELIST_ADDED {kind}={norm}")
    return {"kind": kind, "value": norm}


def _whitelist_remove(entry: str) -> dict:
    kind, norm = _normalize_wl_entry(entry)
    # Update memory
    if kind == "ip":
        _ip_whitelist_ips.discard(norm)
    else:
        # remove all nets equal to norm
        target = ipaddress.ip_network(norm, strict=False)
        _ip_whitelist_nets[:] = [n for n in _ip_whitelist_nets if n != target]
    # Update file list
    items = _persist_whitelist_list()
    if norm in items:
        items = [x for x in items if x != norm]
        _persist_whitelist_write(items)
        logging.getLogger("ExternalAPI.file").warning(f"WHITELIST_REMOVED {kind}={norm}")
        return {"removed": True, "kind": kind, "value": norm}
    return {"removed": False, "kind": kind, "value": norm}

# Sensitive filenames that indicate probing for config files or secrets.
# If repeated within a short window, we will immediately block the offender.
_sensitive_paths = [
    "/wp-config.php",
    "/.env",
    "/config.json",
    "/config.js",
    "/aws-config.js",
    "/aws.config.js",
    "/client_secrets.json",
    "/.gitlab-ci/.env",
    "/.env.example",
]
# Paths exempted from 404 probe-counter increment — these are legitimate
# client poll endpoints that routinely return 404 for unregistered or
# stale miner_keys. Scanner detection continues for all other 404s.
_exempt_404_prefixes = (
    "/credentials/",
    "/installations/",
    "/versions/",
    "/PoC/",
)

def _is_exempt_404_path(url_path: str) -> bool:
    for prefix in _exempt_404_prefixes:
        if url_path.startswith(prefix):
            return True
    return False

# maps ip -> deque[timestamps_of_sensitive_tries]
_sensitive_counters: Dict[str, deque] = defaultdict(lambda: deque())

# Sensitivity thresholds
_sensitive_window = int(os.getenv("SENSITIVE_WINDOW", "30"))
_sensitive_threshold = int(os.getenv("SENSITIVE_THRESHOLD", "3"))

# Add middleware to log HTTP responses with appropriate levels
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = datetime.now()
    client_ip = request.client.host if request.client else "unknown"
    # Fallback: if connecting IP is a trusted proxy and ProxyHeadersMiddleware
    # didn't resolve the real client, extract from X-Real-IP / X-Forwarded-For.
    if client_ip in _trusted_proxy_set:
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            client_ip = real_ip
        else:
            xff = request.headers.get("x-forwarded-for", "")
            if xff:
                client_ip = xff.split(",")[0].strip()
    wl = _is_whitelisted(client_ip)
    # If IP is currently blocked, short-circuit with 403 and log (unless whitelisted)
    now_ts = datetime.now().timestamp()
    blocked_until = _probe_blocklist.get(client_ip)
    if not wl and blocked_until and blocked_until > now_ts:
        # Immediate short-circuit response for blocked IPs
        # Emit a fail2ban-friendly marker so external tools see repeated-block events
        try:
            logging.getLogger("ExternalAPI.file").warning(f"FAILED_PROBE_BLOCK ip={client_ip} blocked_until={blocked_until}")
        except Exception:
            logging.getLogger("ExternalAPI.file").warning("FAILED_PROBE_BLOCK ip=%s blocked", client_ip)
        logging.getLogger("ExternalAPI.console").warning(f"{client_ip} - [WARNING] - 403 BLOCKED - IP temporarily blocked due to repeated probes")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("Forbidden", status_code=403)
    response = await call_next(request)

    # Prevent CDN (BunnyCDN) from caching API responses
    response.headers["Cache-Control"] = "no-store"
    
    # Only log non-static endpoints and important status codes
    url_path = request.url.path
    
    # Skip logging for docs, static files, and health checks
    if url_path in ["/docs", "/openapi.json", "/redoc", "/health"] or url_path.startswith("/static"):
        return response
    
    # Log the request/response with concise format
    duration = (datetime.now() - start_time).total_seconds()
    # client_ip already computed above for early block checks
    method = request.method
    status_code = response.status_code
    # Capture query string and User-Agent for better fingerprinting of probes
    query_string = request.url.query
    user_agent = request.headers.get("user-agent")
    
    # Pull an optional short auth note from the request (set by auth dependencies)
    auth_note = getattr(request.state, "auth_note", None)
    note_suffix = f" - [{auth_note}]" if auth_note and auth_note != "OK" else ""
    wl_suffix = " - [WHITELIST]" if wl else ""

    # Build the concise message. Format required:
    # [timestamp] - <IP> - [LEVEL_LABEL] <status> <METHOD> <PATH> - <AUTH_NOTE>
    # Build an extra suffix with query-string and a shortened user-agent when available
    qs_suffix = " - QS: [REDACTED]" if query_string else ""
    # Shorten User-Agent to avoid long clutter in console logs. Configurable via LOG_UA_MAXLEN.
    if user_agent:
        try:
            max_ua = int(os.getenv("LOG_UA_MAXLEN", "40"))
        except Exception:
            max_ua = 40
        ua_clean = re.sub(r"\s+", " ", user_agent).strip()
        short_ua = ua_clean if len(ua_clean) <= max_ua else ua_clean[: max_ua - 3] + "..."
        ua_suffix = f" - UA: {short_ua}"
    else:
        ua_suffix = ""

    # Prepare two message variants: console (no UA) and file (with UA)
    console_msg_base = f"{client_ip} - [{{}}] - {status_code} {method} {url_path}{note_suffix}{qs_suffix}{wl_suffix}"
    file_msg_base = f"{client_ip} - [{{}}] - {status_code} {method} {url_path}{note_suffix}{qs_suffix}{ua_suffix}{wl_suffix}"

    if status_code >= 500:
        level_label = 'ERROR'
        console_msg = console_msg_base.format(level_label)
        file_msg = file_msg_base.format(level_label)
        logging.getLogger("ExternalAPI.console").error(console_msg)
        logging.getLogger("ExternalAPI.file").error(file_msg)
        # reset consecutive 404s on non-404 status
        try:
            _probe_404_consec[client_ip] = 0
        except Exception:
            pass
    elif status_code >= 400:
        level_label = 'API ACCESS ERROR'
        console_msg = console_msg_base.format(level_label)
        file_msg = file_msg_base.format(level_label)
        logging.getLogger("ExternalAPI.console").log(35, console_msg)
        logging.getLogger("ExternalAPI.file").log(35, file_msg)
        # Track repeated 404/4xx probes and temporarily block if threshold exceeded
        try:
            async with _probe_lock:
                if wl:
                    # Do not track or ban whitelisted IPs
                    pass
                elif status_code == 404:
                    now_ts = datetime.now().timestamp()
                    # Exempt legitimate client poll paths from probe-counter logic.
                    # These endpoints routinely return 404 for unregistered or
                    # stale miner_keys during normal operation. Scanner detection
                    # continues for all OTHER 404 paths (see _exempt_404_prefixes).
                    if _is_exempt_404_path(url_path):
                        # Legit 404; do not count toward probe detection.
                        pass
                    else:
                        dq = _probe_404_counters[client_ip]
                        # Emit a machine-parseable marker that fail2ban can watch for
                        try:
                            logging.getLogger("ExternalAPI.file").warning(f"FAILED_PROBE_404 ip={client_ip} path={url_path} ua=\"{user_agent or ''}\"")
                        except Exception:
                            # safe fallback if formatting fails
                            logging.getLogger("ExternalAPI.file").warning("FAILED_PROBE_404 ip=%s path=%s", client_ip, url_path)
                        dq.append(now_ts)
                        # Trim old timestamps outside the window
                        while dq and dq[0] < now_ts - _probe_404_window:
                            dq.popleft()
                        # Increment consecutive 404 counter
                        c = (_probe_404_consec.get(client_ip, 0) + 1)
                        _probe_404_consec[client_ip] = c
                        # Check for sensitive path probes and handle immediate blocking
                        try:
                            for sensitive in _sensitive_paths:
                                # simple substring match to catch variants
                                if sensitive in url_path:
                                    sc = _sensitive_counters[client_ip]
                                    sc.append(now_ts)
                                    # trim old entries
                                    while sc and sc[0] < now_ts - _sensitive_window:
                                        sc.popleft()
                                    if len(sc) >= _sensitive_threshold:
                                        # timed block (persisted)
                                        _probe_blocklist[client_ip] = now_ts + _probe_block_seconds
                                        _persist_add_ban(client_ip, reason="sensitive_probe")
                                        logging.getLogger("ExternalAPI.file").warning(f"FAILED_SENSITIVE_PROBE ip={client_ip} path={url_path} count={len(sc)}")
                                        logging.getLogger("ExternalAPI.console").warning(f"{client_ip} - [WARNING] - IP blocked {_probe_block_seconds}s due to sensitive probes")
                                        sc.clear()
                                        dq.clear()
                                        _probe_404_consec[client_ip] = 0
                                        break
                        except Exception:
                            logging.getLogger("ExternalAPI.file").debug("sensitive probe detection failed for %s", client_ip)
                        # Check consecutive 404 threshold
                        if c >= _probe_404_consec_threshold:
                            _probe_blocklist[client_ip] = now_ts + _probe_block_seconds
                            _persist_add_ban(client_ip, reason="404_consecutive")
                            logging.getLogger("ExternalAPI.file").warning(f"FAILED_PROBE_CONSEC_404 ip={client_ip} count={c}")
                            logging.getLogger("ExternalAPI.console").warning(f"{client_ip} - [WARNING] - IP blocked {_probe_block_seconds}s due to {c} consecutive 404s")
                            _probe_404_consec[client_ip] = 0
                            dq.clear()
                            _sensitive_counters[client_ip].clear()

                        if len(dq) >= _probe_404_threshold:
                            # Timed block for repeated 404 probes (persisted)
                            _probe_blocklist[client_ip] = now_ts + _probe_block_seconds
                            _persist_add_ban(client_ip, reason="404_threshold")
                            # emit a fail2ban-friendly block marker as well (file-only)
                            logging.getLogger("ExternalAPI.file").warning(f"FAILED_PROBE_BLOCK ip={client_ip} reason=404s count={len(dq)}")
                            # user-visible console message (UA not included)
                            logging.getLogger("ExternalAPI.console").warning(f"{client_ip} - [WARNING] - IP blocked {_probe_block_seconds}s due to {len(dq)} 404s in {_probe_404_window}s")
                            dq.clear()
                            _probe_404_consec[client_ip] = 0
                else:
                    # For other 4xx codes we might optionally clear counters or ignore
                    _probe_404_consec[client_ip] = 0
        except Exception:
            # Non-fatal: don't let tracking break request handling
            logger.debug("probe tracking failed")
    elif status_code >= 300:
        level_label = 'WARNING'
        # If there's a Location header, include redirect target for clarity
        location = response.headers.get('location') or response.headers.get('Location')
        redirect_suffix = f" - Redirect -> {location}" if location else ""
        console_msg = console_msg_base.format(level_label) + redirect_suffix
        file_msg = file_msg_base.format(level_label) + redirect_suffix
        logging.getLogger("ExternalAPI.console").warning(console_msg)
        logging.getLogger("ExternalAPI.file").warning(file_msg)
        # reset consecutive 404s on non-404 status
        try:
            _probe_404_consec[client_ip] = 0
        except Exception:
            pass
    else:
        level_label = 'INFO'
        console_msg = console_msg_base.format(level_label)
        file_msg = file_msg_base.format(level_label)
        logging.getLogger("ExternalAPI.console").info(console_msg)
        logging.getLogger("ExternalAPI.file").info(file_msg)
        # reset consecutive 404s on non-404 status
        try:
            _probe_404_consec[client_ip] = 0
        except Exception:
            pass
    
    return response

# Add rate limit exception handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


# Redirect root to the interactive docs to provide a friendly public entrypoint.
@app.get("/", include_in_schema=False)
def _root_redirect():
    return RedirectResponse(url="/docs")


def _detect_token_role(request: Request) -> str:
    """Detect what role a request has based on Authorization header.
    
    Returns 'admin', 'flxtime', 'general', 'dropwireless', or 'public'.
    """
    try:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return "public"
        token = auth_header[7:].strip()
        
        admin_token = os.getenv("API_BEARER_TOKEN_ADMIN")
        flxtime_token = os.getenv("API_BEARER_TOKEN_FLXTIME")
        general_token = os.getenv("API_BEARER_TOKEN")
        dropwireless_token = os.getenv("API_BEARER_TOKEN_DROPWIRELESS")
        
        # Check admin first (highest privilege)
        if admin_token and token == admin_token:
            return "admin"
        # Check FlxTime
        if flxtime_token and token == flxtime_token:
            return "flxtime"
        # Check general (fallback for FlxTime if needed)
        if general_token and token == general_token:
            # General token can access FlxTime endpoints too
            return "general"
        # Check DropWireless
        if dropwireless_token and token == dropwireless_token:
            return "dropwireless"
        
        return "public"
    except Exception:
        return "public"

# Inject human-readable enum descriptions for MinerCode into the OpenAPI schema.
# This uses a vendor extension `x-enum-descriptions` which Swagger UI will render
# in the schema details. We also set an example for endpoints that reference
# MinerCode via the model-level examples already present in `models.py`.
# (OpenAPI augmentation moved into lifespan handler above.)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@app.get("/admin/blocks", tags=["Admin"], summary="List temporarily blocked IPs")
def list_blocked_ips(token: str = Depends(verify_bearer_token_admin)) -> Dict[str, Any]:
    """Return the current in-memory temporary blocklist.

    Note: this blocklist is in-memory and will reset if the app restarts.
    """
    now_ts = datetime.now().timestamp()
    # return only currently blocked IPs. For permanent bans (infinity) report as permanent.
    blocked: Dict[str, Any] = {}
    for ip, until in _probe_blocklist.items():
        try:
            if until > now_ts:
                # detect permanent bans represented by infinity
                if isinstance(until, float) and until == float("inf"):
                    blocked[ip] = {"permanent": True, "remaining": None}
                else:
                    remaining = max(0, int(until - now_ts))
                    blocked[ip] = {"permanent": False, "remaining": remaining}
        except Exception:
            # Be defensive: if anything odd happens, include the raw value
            blocked[ip] = {"permanent": False, "remaining": str(until)}
    return {"blocked": blocked}



@app.get("/admin/blocks/persistent", tags=["Admin"], summary="List persistent bans")
def list_persistent_bans(token: str = Depends(verify_bearer_token_admin)) -> Dict[str, Any]:
    try:
        bans_file = PathLib("deployment/banned_ips.json")
        if not bans_file.exists():
            return {"banned": []}
        import json
        data = json.loads(bans_file.read_text(encoding="utf-8")) or []
        return {"banned": data}
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to read persistent bans")


@app.post("/admin/blocks/unblock", tags=["Admin"], summary="Unblock an IP")
def unblock_ip(payload: Dict[str, str], token: str = Depends(verify_bearer_token_admin)) -> Dict[str, Any]:
    """Remove an IP from the in-memory blocklist. Expects JSON {"ip": "1.2.3.4"}.

    Emits a fail2ban-friendly UNBLOCK marker for external monitoring.
    """
    ip = payload.get("ip")
    if not ip:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing 'ip' in payload")
    removed = False
    if ip in _probe_blocklist:
        del _probe_blocklist[ip]
        # remove persistent ban as well (if present)
        _persist_remove_ban(ip)
        removed = True
        try:
            logging.getLogger("ExternalAPI.file").warning(f"FAILED_PROBE_UNBLOCK ip={ip}")
        except Exception:
            logging.getLogger("ExternalAPI.file").warning("FAILED_PROBE_UNBLOCK ip=%s", ip)
    return {"unblocked": removed}


# Whitelist admin endpoints (hidden from OpenAPI by default)
@app.get("/admin/whitelist", tags=["Admin"], summary="List whitelisted entries")
def admin_whitelist_list(token: str = Depends(verify_bearer_token_admin)) -> Dict[str, Any]:
    """List effective whitelist entries. Returns normalized IPs and CIDRs."""
    ips = sorted(list(_ip_whitelist_ips))
    cidrs = sorted([str(n) for n in _ip_whitelist_nets])
    # Also include what's in the file for visibility
    file_list = _persist_whitelist_list()
    return {"ips": ips, "cidrs": cidrs, "file": file_list}


@app.post("/admin/whitelist", tags=["Admin"], summary="Add whitelist entry")
def admin_whitelist_add(payload: Dict[str, str], token: str = Depends(verify_bearer_token_admin)) -> Dict[str, Any]:
    entry = (payload or {}).get("entry")
    if not entry:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing 'entry' in payload")
    try:
        result = _whitelist_add(entry)
        return {"ok": True, **result}
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid IP or CIDR")
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to add whitelist entry")


@app.post("/admin/whitelist/remove", tags=["Admin"], summary="Remove whitelist entry")
def admin_whitelist_remove(payload: Dict[str, str], token: str = Depends(verify_bearer_token_admin)) -> Dict[str, Any]:
    entry = (payload or {}).get("entry")
    if not entry:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing 'entry' in payload")
    try:
        result = _whitelist_remove(entry)
        return {"ok": True, **result}
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid IP or CIDR")
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to remove whitelist entry")


@app.get("/health", tags=["Health"], summary="Health check")
def health() -> Dict[str, Any]:
    """Simple health endpoint used by probes and external tunnels.

    Returns a small JSON payload with readiness and runtime information.
    """
    return {
        "ok": True,
        "pid": os.getpid(),
        "port": int(os.getenv("PORT", "8081")),
        "time": utc_now().isoformat(),
    }


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Return 204 No Content for favicon to avoid log noise."""
    from fastapi.responses import Response
    return Response(status_code=204)


# Role-based OpenAPI JSON endpoints (protected)
@app.get("/openapi/admin.json", include_in_schema=False)
def openapi_admin(token: str = Depends(verify_bearer_token_admin)):
    return JSONResponse(_openapi_admin)


@app.get("/openapi/flxtime.json", include_in_schema=False)
def openapi_flxtime(token: str = Depends(verify_bearer_token_flxtime)):
    return JSONResponse(_openapi_flxtime)


@app.get("/openapi/general.json", include_in_schema=False)
def openapi_general(token: str = Depends(verify_bearer_token_general)):
    return JSONResponse(_openapi_general)


@app.get("/openapi/dropwireless.json", include_in_schema=False)
def openapi_dropwireless(token: str = Depends(verify_bearer_token_dropwireless)):
    return JSONResponse(_openapi_dropwireless)


@app.get("/openapi/public.json", include_in_schema=False)
def openapi_public():
    return JSONResponse(_openapi_public)


def _swagger_ui_html_with_auth(openapi_url: str, title: str, current_role: str) -> HTMLResponse:
    """Serve a Swagger UI that automatically attaches Authorization header
    from localStorage for ALL requests (including fetching the OpenAPI JSON).

    This makes role-specific docs work without protecting the docs HTML route
    itself, while still protecting the OpenAPI JSON endpoints.
    
    Auto-redirects to the correct role-specific docs page if a token is found
    that grants access to a different role.
    """
    html = fr"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>{title}</title>
      <link rel="stylesheet" type="text/css" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist/swagger-ui.css" />
      <style>
        html {{ box-sizing: border-box; overflow: -moz-scrollbars-vertical; overflow-y: scroll; }}
        *, *:before, *:after {{ box-sizing: inherit; }}
        body {{ margin:0; background: #fafafa; }}
      </style>
    </head>
    <body>
      <div id="swagger-ui"></div>
      <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist/swagger-ui-bundle.js"></script>
      <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist/swagger-ui-standalone-preset.js"></script>
      <script>
        function findBearerToken() {{
          try {{
            const keys = Object.keys(localStorage);
            for (const k of keys) {{
              const raw = localStorage.getItem(k);
              if (!raw) continue;
              try {{
                const obj = JSON.parse(raw);
                if (obj && typeof obj === 'object' && obj.authorized && typeof obj.authorized === 'object') {{
                  for (const v of Object.values(obj.authorized)) {{
                    if (v && typeof v === 'object' && 'value' in v) {{
                      const token = (v.value || '').toString();
                      if (token) return token.replace(/^Bearer\s+/i, '');
                    }}
                  }}
                }}
                const schemes = ['HTTPBearer', 'bearerAuth', 'Bearer', 'Authorization', 'bearer'];
                for (const s of schemes) {{
                  if (obj[s] && typeof obj[s] === 'object' && obj[s].value) {{
                    const token = (obj[s].value || '').toString();
                    if (token) return token.replace(/^Bearer\s+/i, '');
                  }}
                }}
                if (obj.bearer && obj.bearer.value) {{
                  const token = (obj.bearer.value || '').toString();
                  if (token) return token.replace(/^Bearer\s+/i, '');
                }}
              }} catch (e) {{
                if (typeof raw === 'string' && raw.length > 20) {{
                  const m = raw.match(/Bearer\s+([A-Za-z0-9-_\.+=]+)/);
                  if (m && m[1]) return m[1];
                }}
              }}
            }}
          }} catch (e) {{}}
          return null;
        }}

                // On-demand role detection (no polling). Trigger when token changes.
                let __lastToken = null;
                let __authDebounce = null;

                function roleRedirectIfNeeded(token) {{
                    const currentRole = '{current_role}';
                    if (!token) {{
                        if (currentRole !== 'public') {{
                            window.location.replace('/docs/public');
                        }} else if (!window.swaggerUIInitialized) {{
                            initSwaggerUI(null);
                            window.swaggerUIInitialized = true;
                        }}
                        return;
                    }}
                    fetch('/api/detect-role', {{ headers: {{ 'Authorization': 'Bearer ' + token }} }})
                        .then(r => r.ok ? r.json() : {{ role: 'public' }})
                        .then(data => {{
                            const detectedRole = (data && data.role) || 'public';
                            const roleMap = {{ admin: '/docs/admin', flxtime: '/docs/flxtime', general: '/docs/general', dropwireless: '/docs/dropwireless', public: '/docs/public' }};
                            const targetPath = roleMap[detectedRole] || '/docs/public';
                            if (detectedRole !== currentRole && window.location.pathname !== targetPath) {{
                                window.location.replace(targetPath);
                                return;
                            }}
                            if (!window.swaggerUIInitialized) {{
                                initSwaggerUI(token);
                                window.swaggerUIInitialized = true;
                            }}
                        }})
                        .catch(() => {{
                            if (!window.swaggerUIInitialized) {{
                                initSwaggerUI(token);
                                window.swaggerUIInitialized = true;
                            }}
                        }});
                }}

                function scheduleAuthCheck() {{
                    if (__authDebounce) clearTimeout(__authDebounce);
                    __authDebounce = setTimeout(() => {{
                        const t = findBearerToken();
                        if (t !== __lastToken) {{
                            __lastToken = t;
                            roleRedirectIfNeeded(t);
                        }}
                    }}, 150);
                }}

                // Monkey-patch localStorage to detect token writes/removals (login/logout)
                (function() {{
                    try {{
                        const _set = localStorage.setItem.bind(localStorage);
                        localStorage.setItem = function(k, v) {{ const r = _set(k, v); scheduleAuthCheck(); return r; }};
                        const _remove = localStorage.removeItem.bind(localStorage);
                        localStorage.removeItem = function(k) {{ const r = _remove(k); scheduleAuthCheck(); return r; }};
                    }} catch (e) {{ /* ignore */ }}
                }})();

                // Also listen for cross-tab changes
                window.addEventListener('storage', scheduleAuthCheck);

                // Initial check once on load (no polling afterwards)
                window.onload = function() {{
                    __lastToken = findBearerToken();
                    roleRedirectIfNeeded(__lastToken);
                }}

        function initSwaggerUI(token) {{
          const ui = SwaggerUIBundle({{
            url: '{openapi_url}',
            dom_id: '#swagger-ui',
            deepLinking: true,
            presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
            layout: 'BaseLayout',
            persistAuthorization: true,
            requestInterceptor: function(req) {{
              const t = token || findBearerToken();
              if (t) {{ req.headers['Authorization'] = 'Bearer ' + t; }}
              return req;
            }}
          }});

          // Attempt to preauthorize common scheme names so the UI shows as authorized
          if (token && ui && ui.preauthorizeApiKey) {{
            const bearerValue = 'Bearer ' + token;
            const schemes = ['HTTPBearer', 'bearerAuth', 'Bearer'];
            for (const s of schemes) {{
              try {{ ui.preauthorizeApiKey(s, bearerValue); }} catch (e) {{}}
            }}
          }}
        }}
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)
@app.get("/docs/admin", include_in_schema=False)
def docs_admin():
    return _swagger_ui_html_with_auth(
        openapi_url="/openapi/admin.json",
        title="Admin API Docs",
        current_role="admin"
    )


@app.get("/docs/flxtime", include_in_schema=False)
def docs_flxtime():
    return _swagger_ui_html_with_auth(
        openapi_url="/openapi/flxtime.json",
        title="FlxTime API Docs",
        current_role="flxtime"
    )


@app.get("/docs/general", include_in_schema=False)
def docs_general():
    return _swagger_ui_html_with_auth(
        openapi_url="/openapi/general.json",
        title="General API Docs",
        current_role="general"
    )


@app.get("/docs/dropwireless", include_in_schema=False)
def docs_dropwireless():
    return _swagger_ui_html_with_auth(
        openapi_url="/openapi/dropwireless.json",
        title="DropWireless API Docs",
        current_role="dropwireless"
    )


@app.get("/docs", include_in_schema=False)
def docs_public(request: Request):
    """Smart docs endpoint that serves role-specific schema based on token."""
    # Return custom HTML that checks for stored token and loads appropriate schema
    html_content = r"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>API Documentation</title>
        <script>
            // Attempt to locate a persisted Swagger UI bearer token across common storage keys
            function findBearerTokenInLocalStorage() {
                try {
                    const keys = Object.keys(localStorage);
                    for (const k of keys) {
                        const raw = localStorage.getItem(k);
                        if (!raw) continue;
                        // Try JSON parse; many swagger plugins store JSON structures
                        try {
                            const obj = JSON.parse(raw);
                            // 1) swagger-ui authorized map pattern: obj.authorized.{scheme}.value
                            if (obj && typeof obj === 'object' && obj.authorized && typeof obj.authorized === 'object') {
                                for (const v of Object.values(obj.authorized)) {
                                    if (v && typeof v === 'object' && 'value' in v) {
                                        const token = (v.value || '').toString();
                                        if (token) return token.replace(/^Bearer\s+/i, '');
                                    }
                                }
                            }
                            // 2) direct scheme name buckets (bearerAuth/Bearer/etc)
                            const schemes = ['bearerAuth', 'Bearer', 'http', 'HTTP', 'Authorization', 'bearer'];
                            for (const s of schemes) {
                                if (obj[s] && typeof obj[s] === 'object' && obj[s].value) {
                                    const token = (obj[s].value || '').toString();
                                    if (token) return token.replace(/^Bearer\s+/i, '');
                                }
                            }
                            // 3) some plugins store a flat { bearer: { value } }
                            if (obj.bearer && obj.bearer.value) {
                                const token = (obj.bearer.value || '').toString();
                                if (token) return token.replace(/^Bearer\s+/i, '');
                            }
                        } catch (e) {
                            // Not JSON; try to heuristically extract a token-like string
                            if (typeof raw === 'string' && raw.length > 20) {
                                const m = raw.match(/Bearer\s+([A-Za-z0-9-_\.=]+)/);
                                if (m && m[1]) return m[1];
                            }
                        }
                    }
                } catch (e) {
                    // ignore
                }
                return null;
            }

            // Also allow passing ?token=... for manual override
            function getTokenFromQuery() {
                try {
                    const params = new URLSearchParams(window.location.search);
                    const t = params.get('token');
                    return t && t.trim() ? t.trim() : null;
                } catch (e) { return null; }
            }

            (function init() {
                const queryToken = getTokenFromQuery();
                const storedToken = findBearerTokenInLocalStorage();
                const token = queryToken || storedToken;

                if (token) {
                    fetch('/api/detect-role', { headers: { 'Authorization': 'Bearer ' + token } })
                        .then(r => r.ok ? r.json() : { role: 'public' })
                        .then(data => {
                            if (data.role === 'admin') {
                                window.location.replace('/docs/admin');
                            } else if (data.role === 'flxtime') {
                                window.location.replace('/docs/flxtime');
                            } else if (data.role === 'general') {
                                window.location.replace('/docs/general');
                            } else if (data.role === 'dropwireless') {
                                window.location.replace('/docs/dropwireless');
                            } else {
                                window.location.replace('/docs/public');
                            }
                        })
                        .catch(() => window.location.replace('/docs/public'));
                } else {
                    window.location.replace('/docs/public');
                }
            })();
        </script>
    </head>
    <body>
        <p>Loading documentation...</p>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/docs/public", include_in_schema=False)
def docs_public_direct():
    """Public docs without auth."""
    return _swagger_ui_html_with_auth(
        openapi_url="/openapi/public.json",
        title="Public API Docs",
        current_role="public"
    )


@app.get("/api/detect-role", include_in_schema=False)
def detect_role_endpoint(request: Request):
    """Helper endpoint to detect token role."""
    role = _detect_token_role(request)
    return {"role": role}


@app.post("/admin/openapi/refresh", tags=["Admin"], summary="Refresh OpenAPI caches")
def admin_refresh_openapi(token: str = Depends(verify_bearer_token_admin)) -> Dict[str, Any]:
    """Rebuild the role-scoped OpenAPI caches. Useful after hot-reload or code updates."""
    _rebuild_openapi_caches(app)
    try:
        total_paths = len((_openapi_full or {}).get("paths", {}))
    except Exception:
        total_paths = 0
    return {"ok": True, "paths": total_paths}



@app.get(
    "/versions/{miner_code}",
    response_model=VersionResponse,
    response_model_exclude_none=True,
    summary="Get required miner version",
    tags=["Versions"],
)
def get_required_version(
    miner_code: MinerCode = Path(...),
    platform: Optional[str] = Query(None, description="Optional platform filter ('windows', 'linux', 'test-windows', 'test-linux')"),
    token: str = Depends(verify_bearer_token_general)
) -> VersionResponse:
    try:
        normalized_platform = None
        if platform is not None:
            normalized_platform = (platform or "").strip().lower().replace("_", "-")
            if normalized_platform not in ("windows", "linux", "test-windows", "test-linux"):
                raise ValueError("Invalid platform. Use 'windows', 'linux', 'test-windows', or 'test-linux'.")

        version_data = STORE.get_version_document(miner_code.value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unable to load version data")

    if version_data is None:
        return VersionResponse(miner_code=miner_code.value, detail="none")

    if normalized_platform:
        platform_section = version_data.get(normalized_platform)
        # If no platform-specific data exists, return a 200 with an empty/partial response
        # rather than a 404. This makes clients simpler when a platform has no requirements.
        filtered: Dict[str, Any] = {"miner_code": version_data.get("miner_code", miner_code.value)}
        if version_data.get("_id") is not None:
            filtered["_id"] = version_data.get("_id")
        if platform_section:
            filtered[normalized_platform] = platform_section
        else:
            filtered["detail"] = f"No version data for platform '{normalized_platform}'"
        return VersionResponse(**filtered)

    return VersionResponse(**version_data)


@app.get(
    "/installers/{os}/supported",
    response_model=InstallerSupportResponse,
    summary="List miner codes with installers for the given OS",
    tags=["Installers"],
)
def list_supported_installers(
    os: str = Path(..., description="Operating system identifier (e.g., 'windows', 'linux', 'test-windows', 'test-linux')"),
    token: str = Depends(verify_bearer_token_general)
) -> InstallerSupportResponse:
    normalized = (os or "").strip().lower()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Operating system must be provided",
        )

    try:
        miner_codes = STORE.list_supported_installers(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return InstallerSupportResponse(os=normalized, miner_codes=miner_codes)


@app.put(
    "/admin/versions/{miner_code}",
    response_model=VersionResponse,
    summary="Update required miner version",
    tags=["Admin"],
)
def update_required_version(
    miner_code: MinerCodeOrAll = Path(...),
    update: UpdateVersionRequest = Body(...),
    token: str = Depends(verify_bearer_token_admin)
) -> VersionResponse:
    """Update platform-scoped version requirements for a miner code or all miners."""

    def _dump(model_obj: Any) -> Dict[str, Any]:
        if model_obj is None:
            return {}
        if hasattr(model_obj, "model_dump"):
            return model_obj.model_dump(exclude_none=True, by_alias=True)  # pydantic v2
        return model_obj.dict(exclude_none=True, by_alias=True)  # type: ignore[attr-defined]

    update_payload: Dict[str, Any] = {}
    for attr, alias in (
        ("windows", "windows"),
        ("linux", "linux"),
        ("test_windows", "test-windows"),
        ("test_linux", "test-linux"),
    ):
        section = getattr(update, attr, None)
        data = _dump(section)
        if data:
            update_payload[alias] = data

    if update.limit is not None:
        update_payload["limit"] = update.limit

    if not update_payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one platform section or limit must be provided"
        )

    # Handle ALL case - update all miner codes
    if miner_code.value == "ALL":
        all_codes = [code.value for code in MinerCode]
        last_doc: Optional[Dict[str, Any]] = None
        for code in all_codes:
            try:
                last_doc = STORE.update_version_document(code, update_payload)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        response_payload = dict(update_payload)
        response_payload["miner_code"] = "ALL"
        if last_doc and last_doc.get("_id") is not None:
            response_payload["_id"] = last_doc.get("_id")
        return VersionResponse(**response_payload)

    # Handle single miner code case
    try:
        updated_doc = STORE.update_version_document(miner_code.value, update_payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return VersionResponse(**updated_doc)


@app.put(
    "/admin/versions/{miner_code}/rewards",
    response_model=RewardsParamsResponse,
    summary="Set BM rewards multiplier parameters",
    tags=["Admin"],
)
def update_rewards_params(
    miner_code: MinerCode = Path(...),
    body: RewardsParamsRequest = Body(...),
    token: str = Depends(verify_bearer_token_admin)
) -> RewardsParamsResponse:
    """Set multiplier_base and multiplier_per_tool on the version document for a miner code."""
    try:
        STORE.update_rewards_params(miner_code.value, body.multiplier_base, body.multiplier_per_tool)
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update rewards parameters")

    return RewardsParamsResponse(
        miner_code=miner_code.value,
        multiplier_base=body.multiplier_base,
        multiplier_per_tool=body.multiplier_per_tool,
    )


@app.get(
    "/credentials/{miner_key}",
    response_model=MinerProfileResponse,
    summary="Get miner credentials/profile",
    tags=["Credentials"],
)
def get_miner_profile(
    miner_key: str = Path(..., description="Full miner key"),
    token: str = Depends(verify_bearer_token_general)
) -> MinerProfileResponse:
    profile = STORE.get_miner_profile(miner_key)
    if not profile.get("exists"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Miner key '{miner_key}' not found in credentials store"
        )
    # Default verified to False if not present
    profile.setdefault("verified", False)
    return MinerProfileResponse(**profile)


@app.get(
    "/credentials/{miner_key}/exists",
    response_model=ExistsResponse,
    summary="Check if miner_key exists",
    tags=["FlxTime"],
)
@limiter.limit(os.getenv("FLXTIME_RATE_LIMIT", "100/minute"))
def check_miner_exists(
    request: Request,
    miner_key: str = Path(..., description="Full miner key"),
    token: str = Depends(verify_bearer_token_flxtime)
) -> ExistsResponse:
    """Check whether a miner_key exists in the credentials database.

    This endpoint is specifically designed for FlxTime partner integration.

    Returns {"exists": true} if found, {"exists": false} otherwise.
    """
    profile = STORE.get_miner_profile(miner_key)
    exists = profile.get("exists", False)
    return ExistsResponse(exists=exists)


@app.get(
    "/credentials/{miner_key}/verified",
    response_model=VerifiedStatusResponse,
    summary="Get miner verification status",
    tags=["Credentials"],
)
def get_miner_verified_status(
    miner_key: str = Path(..., description="Full miner key"),
    token: str = Depends(verify_bearer_token_general)
) -> VerifiedStatusResponse:
    """Get the verification status of a miner.

    Returns the miner_key and its verified status (true/false).
    Defaults to false if the verified field is not set.
    """
    profile = STORE.get_miner_profile(miner_key)
    if not profile.get("exists"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Miner key '{miner_key}' not found in credentials store"
        )
    return VerifiedStatusResponse(
        miner_key=miner_key,
        verified=profile.get("verified", False)
    )


@app.post(
    "/installations/{miner_key}/installations/{install_id}",
    response_model=GenericOk,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upsert installation heartbeat",
    tags=["Installations"],
)
def upsert_installation(
    miner_key: str,
    install_id: str,
    heartbeat: InstallationHeartbeat,
    token: str = Depends(verify_bearer_token_general)
) -> GenericOk:
    if heartbeat.miner_key != miner_key or heartbeat.install_id != install_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body miner identity mismatch")
    payload = heartbeat.model_dump()
    payload.setdefault("last_seen_at", utc_now().isoformat())
    STORE.upsert_installation(miner_key, install_id, payload)
    return GenericOk()


@app.get(
    "/measurements/{hex_id}",
    response_model=Dict[str, Any],
    summary="Query measurements by date range",
    tags=["Measurements"],
)
def query_measurements(
    hex_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    measurement_types: Optional[str] = None,
    token: str = Depends(verify_bearer_token_general)
) -> Dict[str, Any]:
    """Return all measurements for a hex within an optional [start, end] inclusive interval.

    - Dates are ISO 8601 (e.g., 2025-11-10T00:00:00Z). If only `start` is provided, results are from start onward.
    - If only `end` is provided, results are up to that time.
    - Optionally filter by measurement_types (comma-separated, e.g., "bandwidth,satellite").
    
    Returns a document with hex_id and arrays of measurements organized by type:
    {
        "hex_id": "8c2a1072b18cdff",
        "bandwidth": [{...}, {...}],
        "satellite": [{...}, {...}],
        ...
    }
    """
    # Parse date strings defensively
    def _parse_iso(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid datetime: {s}")

    start_dt = _parse_iso(start)
    end_dt = _parse_iso(end)
    
    # Parse measurement_types filter
    types_list = None
    if measurement_types:
        types_list = [t.strip() for t in measurement_types.split(",") if t.strip()]

    result = STORE.get_measurements(
        hex_id=hex_id,
        start=start_dt,
        end=end_dt,
        measurement_types=types_list,
    )

    return result


@app.delete(
    "/installations/{miner_key}/installations/{install_id}",
    response_model=GenericOk,
    summary="Remove installation record",
    tags=["Installations"],
)
def delete_installation(
    miner_key: str,
    install_id: str,
    token: str = Depends(verify_bearer_token_general)
) -> GenericOk:
    """Remove an installation record when a miner is uninstalled from a device.
    
    This will delete the installation record and clean up any associated leases.
    Returns 404 if the installation record is not found.
    """
    deleted = STORE.delete_installation(miner_key, install_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Installation record not found for miner_key={miner_key}, install_id={install_id}"
        )
    return GenericOk()


@app.get(
    "/installations/ip/{external_ip}/status",
    response_model=Dict[str, Any],
    summary="Check external IP installation status across all miner types",
    tags=["Installations"],
)
def check_ip_installation_status(
    external_ip: str = Path(..., description="External IP address to check"),
    token: str = Depends(verify_bearer_token_general),
) -> Dict[str, Any]:
    """Check active installations on an external IP, grouped by miner type.

    Returns installation counts, limits, and details for each miner type
    that has active installations on the given IP.
    """
    # Validate external IP format
    try:
        ipaddress.ip_address(external_ip)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid IP address")

    # Find all installations on this IP
    installations: List[Dict[str, Any]] = []
    try:
        installations = STORE.find_installations_by_external_ip(external_ip)
    except Exception:
        installations = []

    # Group installations by miner type (derived from miner_key prefix or minerCode)
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for inst in installations:
        miner_key = inst.get("miner_key", "")
        miner_code = inst.get("minerCode")
        if not miner_code:
            # Derive from miner_key prefix (e.g. "BM-abc123" -> "BM")
            miner_code = miner_key.split("-")[0] if "-" in miner_key else miner_key[:3]
        by_type.setdefault(miner_code, []).append(inst)

    # Build response with limit from version metadata
    installations_by_type: Dict[str, Any] = {}
    for miner_code, insts in by_type.items():
        # Look up limit from version metadata
        limit_value: Any = "no"
        try:
            version_doc = STORE.get_version_document(miner_code)
            if version_doc and version_doc.get("limit") is not None:
                limit_value = version_doc["limit"]
        except Exception:
            pass

        installations_by_type[miner_code] = {
            "count": len(insts),
            "limit": limit_value,
            "details": insts,
        }

    return {
        "external_ip": external_ip,
        "installations_by_type": installations_by_type,
    }


@app.post(
    "/installations/{miner_key}/leases/{install_id}",
    response_model=LeaseResponse,
    summary="Acquire a mining lease",
    tags=["Leases"],
)
def acquire_installation_lease(
    miner_key: str,
    install_id: str,
    action: LeaseAction = Body(default_factory=LeaseAction),
    token: str = Depends(verify_bearer_token_leases)
) -> LeaseResponse:
    granted, record = STORE.acquire_lease(miner_key, install_id, action.lease_seconds, external_ip=action.external_ip)
    # Use the returned LeaseRecord (if provided) to avoid an extra status DB call.
    if record:
        expires_at = getattr(record, "expires_at", None)
        expires_iso = None
        ttl = 0
        try:
            if isinstance(expires_at, str):
                expires_dt = datetime.fromisoformat(expires_at)
            else:
                expires_dt = expires_at
            if isinstance(expires_dt, datetime) and expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            if isinstance(expires_dt, datetime):
                expires_iso = expires_dt.isoformat()
                ttl = max(0, int((expires_dt - utc_now()).total_seconds()))
        except Exception:
            expires_iso = None
            ttl = 0
        status_payload = {"active": bool(granted), "holder_install_id": getattr(record, "holder_install_id", None), "expires_at": expires_iso, "ttl_seconds": ttl}
        # BM IP conflict: if denied and the conflicting record belongs to a different miner_key, it's an IP conflict
        error_code = None
        if not granted and miner_key.startswith("BM") and action.external_ip and getattr(record, "miner_key", None) != miner_key:
            error_code = "IP_ALREADY_REGISTERED"
        return LeaseResponse(granted=granted, error_code=error_code, **status_payload)
    # fallback: ask the store for status
    logger.debug(f"acquire_installation_lease: FALLBACK calling STORE.lease_status for {miner_key}/{install_id}")
    status_payload = STORE.lease_status(miner_key)
    return LeaseResponse(granted=granted, **status_payload)


@app.patch(
    "/installations/{miner_key}/leases/{install_id}",
    response_model=LeaseResponse,
    summary="Renew a mining lease",
    tags=["Leases"],
)
def renew_installation_lease(
    miner_key: str,
    install_id: str,
    action: LeaseAction = Body(default_factory=LeaseAction),
    token: str = Depends(verify_bearer_token_leases)
) -> LeaseResponse:
    granted, record = STORE.renew_lease(miner_key, install_id, action.lease_seconds, external_ip=action.external_ip)
    # Use returned LeaseRecord to avoid an extra status call when possible
    if record:
        expires_at = getattr(record, "expires_at", None)
        expires_iso = None
        ttl = 0
        try:
            if isinstance(expires_at, str):
                expires_dt = datetime.fromisoformat(expires_at)
            else:
                expires_dt = expires_at
            if isinstance(expires_dt, datetime) and expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            if isinstance(expires_dt, datetime):
                expires_iso = expires_dt.isoformat()
                ttl = max(0, int((expires_dt - utc_now()).total_seconds()))
        except Exception:
            expires_iso = None
            ttl = 0
        status_payload = {"active": bool(granted), "holder_install_id": getattr(record, "holder_install_id", None), "expires_at": expires_iso, "ttl_seconds": ttl}
        return LeaseResponse(granted=granted, **status_payload)
    logger.debug(f"renew_installation_lease: FALLBACK calling STORE.lease_status for {miner_key}/{install_id}")
    status_payload = STORE.lease_status(miner_key)
    return LeaseResponse(granted=granted, **status_payload)


@app.get(
    "/installations/{miner_key}/leases/current",
    response_model=LeaseResponse,
    summary="Get current lease status",
    tags=["Leases"],
)
def lease_status(
    miner_key: str,
    token: str = Depends(verify_bearer_token_leases)
) -> LeaseResponse:
    status_payload = STORE.lease_status(miner_key)
    # Expose False if no active lease.
    return LeaseResponse(granted=status_payload.get("active", False), **status_payload)


@app.get(
    "/PoC/{miner_key}/hardware",
    response_model=HardwareResponse,
    summary="Get PoC hardware document",
    tags=["PoC"],
)
def get_hardware_doc(
    miner_key: str,
    token: str = Depends(verify_bearer_token_general)
) -> HardwareResponse:
    doc = STORE.get_hardware_doc(miner_key)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No hardware document")
    return HardwareResponse(document=doc)


@app.put(
    "/PoC/{miner_key}/hardware",
    response_model=GenericOk,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Replace PoC hardware document",
    tags=["PoC"],
)
def put_hardware_doc(
    miner_key: str,
    payload: HardwareDocument,
    token: str = Depends(verify_bearer_token_general)
) -> GenericOk:
    document = dict(payload.document)
    document.setdefault("miner_key", miner_key)
    document.setdefault("lastUpdated", utc_now().isoformat())
    STORE.put_hardware_doc(miner_key, document)
    return GenericOk()


@app.put(
    "/presearch/ip/{ip}",
    response_model=GenericOk,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Store Presearch node data for an IP",
    tags=["Presearch"],
)
def put_presearch_ip(
    ip: str,
    payload: PresearchPayload,
    token: str = Depends(verify_bearer_token_general)
) -> GenericOk:
    document = payload.model_dump() if hasattr(payload, 'model_dump') else payload.dict()
    document["ip"] = ip
    document.setdefault("timestamp", utc_now().isoformat())
    STORE.put_presearch(ip, document)
    return GenericOk()


@app.put(
    "/mysterium/keystore",
    response_model=GenericOk,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Store Mysterium keystore",
    tags=["Mysterium"],
)
def put_mysterium_keystore(
    payload: MysteriumKeystoreRequest,
    token: str = Depends(verify_bearer_token_general)
) -> GenericOk:
    miner_key = (payload.miner_key or "").strip()
    keystore_b64 = (payload.keystore_b64 or "").strip()
    identity_id = (payload.identity_id or "").strip() or None

    if not miner_key or not keystore_b64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="miner_key and keystore_b64 are required",
        )

    STORE.upsert_mysterium_keystore(
        miner_key=miner_key,
        keystore_b64=keystore_b64,
        identity_id=identity_id,
    )
    return GenericOk()


@app.get(
    "/mysterium/keystore",
    response_model=MysteriumKeystoreResponse,
    response_model_exclude_none=True,
    summary="Fetch Mysterium keystore",
    tags=["Mysterium"],
)
def get_mysterium_keystore(
    miner_key: str = Query(..., description="Full miner key whose keystore should be returned"),
    token: str = Depends(verify_bearer_token_general)
) -> MysteriumKeystoreResponse:
    normalized_key = (miner_key or "").strip()
    if not normalized_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="miner_key query parameter is required",
        )

    record = STORE.get_mysterium_keystore(normalized_key)
    if not record or not record.get("keystore_b64"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No keystore found for the provided miner_key",
        )

    return MysteriumKeystoreResponse(
        keystore_b64=record.get("keystore_b64") or "",
        identity_id=record.get("identity_id"),
    )


@app.post(
    "/measurements/{hex_id}",
    response_model=GenericOk,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload measurement data",
    tags=["Measurements"],
)
def upload_measurements(
    hex_id: str = Path(..., description="H3 hex cell ID (registered location)"),
    payload: MeasurementUpload = Body(...),
    token: str = Depends(verify_bearer_token_general)
) -> GenericOk:
    """Upload measurement data for a specific hex location.
    
    This endpoint allows multiple miner types per hex location and tracks each
    miner instance by install_id to handle multiple miners of the same type.
    
    Data is organized by hex_id with nested arrays per measurement_type.
    Each measurement contains: timestamp, miner_code, install_id, and value.
    
    Example payload:
    {
        "miner_code": "BM",
        "install_id": "abc123",
        "timestamp": "2025-11-15T12:00:00Z",
        "measurement_type": "bandwidth",
        "value": {"upload_mbps": 100, "download_mbps": 500}
    }
    """
    STORE.upload_measurement(
        hex_id=hex_id,
        miner_code=payload.miner_code.value,
        install_id=payload.install_id,
        timestamp=payload.timestamp,
        measurement_type=payload.measurement_type,
        value=payload.value,
    )
    
    return GenericOk()


if __name__ == "__main__":
    import os
    import uvicorn

    # Allow runtime configuration via environment variables.
    # PORT - port number (defaults to 8080)
    # HOST - host to bind (defaults to 0.0.0.0 for production)
    # UVICORN_RELOAD - if set to '1' or 'true' (case-insensitive), enable reload (disabled by default for production)
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    reload_env = os.getenv("UVICORN_RELOAD", "false").lower()
    reload_flag = reload_env in ("1", "true", "yes")

    logger.info(f"Starting server on {host}:{port} (reload={reload_flag})")
    
    # Configure uvicorn to use our existing logging configuration
    # We'll disable uvicorn's log_config to preserve our colored formatting
    uvicorn.run(
        "app:app", 
        host=host, 
        port=port, 
        reload=reload_flag,
        log_config=None,  # Use existing logging configuration
        access_log=False,  # Disable uvicorn access logging - we handle it in middleware
        use_colors=False   # Disable uvicorn colors since we handle our own
    )
