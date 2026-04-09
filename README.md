# External API Service

Lightweight FastAPI service that provides the HTTP contract consumed by the miner binaries. Uses MongoDB for persistent storage with optimized database structure for scalability and performance.

## Project Structure

```
ExternalAPI/
├── app.py                     # Main FastAPI application
├── models.py                  # Data models and schemas
├── storage.py                 # Storage backend (in-memory/MongoDB)
├── requirements.txt           # Python dependencies
├── Dockerfile                # Container image definition
├── docker-compose.yml        # Docker Compose configuration
├── op-entrypoint.sh          # Runtime op wrapper (Docker)
├── README.md                 # This file
└── deployment/               # Deployment files
    ├── deploy.sh            # Automated VPS deployment script
    ├── quick-deploy.sh      # One-liner deployment script
    ├── start_with_secrets.sh # Host startup script with 1Password service account token
    ├── ecosystem.config.js  # PM2 process manager configuration
    ├── hardware_exe_api.service # Systemd service definition
    └── fail2ban            # Optional fail2ban integration
```

## Quick start (Development)

```powershell
cd ExternalAPI
python -m venv .venv
# Windows PowerShell
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# Start the development server
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8080
```

## Production Deployment (VPS)

### Prerequisites
- Ubuntu/Debian VPS with root access
- Domain name pointing to your VPS IP (optional but recommended)

### Automated Deployment

1. **Upload files to your VPS:**
   ```bash
   # On your VPS, create temporary directory (quick deploy location)
   mkdir -p /tmp/hardware_exe_api
   
   # Upload all files (use scp, rsync, or git clone)
   scp -r ./* user@your-vps:/tmp/hardware_exe_api/
   # OR clone from git
   git clone https://github.com/Fry-Foundation/ExternalAPI.git /tmp/hardware_exe_api
   ```

2. **Run the deployment script:**
   ```bash
   cd /tmp/hardware_exe_api
   chmod +x deployment/deploy.sh
   sudo ./deployment/deploy.sh
   ```

3. **Configure your environment:**
   ```bash
   # Edit production configuration
   sudo nano /opt/hardware_exe_api/.env
   
   # Update nginx server name
   sudo nano /etc/nginx/sites-available/hardwareapi
   
   # Restart services
   sudo systemctl restart hardware_exe_api nginx
   ```

### Manual Deployment Options

#### Option 0: Docker (Recommended)
```bash
# 1) Ensure the 1Password service account token file exists on the host:
#    /etc/opt/hardwareapi/op_service_account_token (root:root, 0400)
#
# 2) If you bind-mount ./logs into the container, ensure it is writable
#    by the container user (UID 10001):
#    sudo chown -R 10001:10001 /path/to/your/repo/logs
#    sudo chmod 0750 /path/to/your/repo/logs
#
# 2) Configure environment in docker-compose.yml (op:// references).
#
# 3) Build and run
docker compose up -d --build
```

#### Option 1: Using Secure Startup Script (Recommended)
```bash
# Start the application with secure secret management
./deployment/start_with_secrets.sh
```
This script automatically:
- Resolves MongoDB connection string from 1Password
- Sets up all environment variables securely
- Starts the application with proper configuration

#### Option 2: Using systemd
```bash
# Start/stop/restart the service
sudo systemctl start hardware_exe_api
sudo systemctl stop hardware_exe_api
sudo systemctl restart hardware_exe_api

# View logs
sudo journalctl -u hardware_exe_api -f
```

#### Option 3: Using PM2
```bash
cd /opt/hardware_exe_api
pm2 start deployment/ecosystem.config.js
pm2 save
pm2 startup  # Follow instructions to auto-start on boot

# PM2 commands
pm2 status
pm2 logs hardware_exe_api
pm2 restart hardware_exe_api
```

### SSL Setup (Recommended)
```bash
# Install certbot
sudo apt install certbot python3-certbot-nginx

# Get SSL certificate (replace with your domain)
sudo certbot --nginx -d your-domain.com

# Auto-renewal is set up automatically
```

## Environment Configuration

### Docker (Compose)
Configure environment variables directly in `docker-compose.yml` using `op://` references. The container entrypoint reads the service account token from `/run/secrets/op_service_account_token` and runs `op run -- python app.py`.

If you are running behind nginx on the same host, set `TRUSTED_PROXY_IPS` to the Docker bridge IP (e.g., `172.17.0.1`) plus loopback addresses.

### Non-Docker (Host)
You can export environment variables or use a local `.env` (not committed):

```env
# Production environment configuration
PORT=8080
HOST=0.0.0.0
UVICORN_RELOAD=false

# MongoDB configuration (REQUIRED - application will fail to start without this)
MONGODB_URI=mongodb://localhost:27017

# 1Password secrets (if using 1Password CLI)
# MONGODB_URI=op://vault/item/field
```

### Development Environment

For local development, create a `.env` file with development settings:

```env
PORT=8080
HOST=127.0.0.1
UVICORN_RELOAD=true
MONGODB_URI=mongodb://localhost:27017
```

Start the development server:

```bash
python app.py
# or explicitly with uvicorn
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8080
```

The miner should be configured with `api_base_url` pointing at your production URL (e.g., `https://your-domain.com` or `http://your-vps-ip:8080`).

## API Endpoints

### Version Management
| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/versions/{miner_code}` | Returns the latest required version for a miner family. |

### Credential Management
| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/credentials/{miner_key}` | Retrieves miner credentials and profile data from creds database. |

### Installation Management
| Method | Path | Description |
| ------ | ---- | ----------- |
| POST | `/installations/{miner_key}/installations/{install_id}` | Upserts per-installation heartbeat information. |

### Lease Management
| Method | Path | Description |
| ------ | ---- | ----------- |
| POST | `/installations/{miner_key}/leases/{install_id}` | Attempts to acquire a global mining lease. |
| PATCH | `/installations/{miner_key}/leases/{install_id}` | Renews an existing lease. |
| GET | `/installations/{miner_key}/leases/current` | Returns the active lease (if any) including remaining TTL. |

### Hardware/PoC Management
| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/PoC/{miner_key}/hardware` | Returns the latest hardware aggregate document from PoC database. |
| PUT | `/PoC/{miner_key}/hardware` | Replaces the hardware aggregate document in PoC database. |

### Database Structure
The API uses a simplified MongoDB structure:
- **PoC Database**: Stores versions, installations, and hardware data
- **creds Database**: Stores miner credentials and profile information

**Note**: Lease history functionality has been removed to prevent document bloat. Current lease status and expiration times provide sufficient tracking information.

## Recent Optimizations

### Database Structure Improvements
- **Simplified Configuration**: Removed `MONGODB_DB` requirement - now uses fixed database names
- **Performance**: Eliminated lease history tracking to prevent document bloat in installations collection
- **Clarity**: Direct database targeting - PoC for operational data, creds for credentials

### API Structure Improvements
- **Organized Endpoints**: Restructured URLs to clearly separate concerns:
  - `/credentials/*` - Credential and profile management
  - `/installations/*` - Installation tracking and lease management  
  - `/PoC/*` - Hardware and Proof-of-Coverage data
- **Removed Endpoints**: Eliminated unused lease history endpoint
- **Cleaner Codebase**: ~30% code reduction through cleanup and simplification

## 1Password Secrets Integration

You can keep secrets (like `MONGODB_URI`) out of plain text by using the 1Password CLI reference format in your environment.

Supported formats:
```env
MONGODB_URI=op/Vault/Item/field
# or
MONGODB_URI=op://Vault/Item/field
```

On startup the app will try to resolve any `op/...` or `op://...` values using the `op read` command and replace the environment variable with the secret value. Make sure the `op` CLI is installed and that `OP_SERVICE_ACCOUNT_TOKEN` is set (the host script reads it from `/etc/opt/hardwareapi/op_service_account_token`). If `op` is not available or the read fails, the original env value is left unchanged.

## Monitoring and Maintenance

### Log Files
- **systemd logs:** `sudo journalctl -u hardware_exe_api -f`
- **PM2 logs:** `pm2 logs hardware_exe_api`
- **Nginx logs:** `sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log`

### Health Checks
The API automatically exposes all endpoints for health monitoring. You can set up monitoring tools to check:
- `GET /credentials/{test_key}` - Basic API functionality
- `GET /installations/{test_key}/leases/current` - Lease system functionality
- HTTP response times and status codes
- System resource usage

### Updates and Maintenance
```bash
# Update application code
cd /opt/hardware_exe_api
git pull origin main  # if using git
sudo systemctl restart hardware_exe_api

# Update system packages
sudo apt update && sudo apt upgrade -y
sudo reboot  # if kernel updates were installed
```

All responses follow the schema documented in `models.py`. Extend the storage layer or swap it with a database-backed implementation as needed.
