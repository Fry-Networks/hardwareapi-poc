#!/bin/bash

# Fry Networks External API VPS Deployment Script
# This script sets up the external API service on a fresh Ubuntu/Debian VPS

set -e

# Configuration
APP_NAME="hardware_exe_api"
APP_DIR="/opt/$APP_NAME"
SERVICE_USER="www-data"
LOG_DIR="/var/log/$APP_NAME"
BRANCH="${BRANCH:-main}"

echo "🚀 Starting deployment of $APP_NAME..."

# Update system packages
echo "📦 Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Install required system packages
echo "🔧 Installing system dependencies..."
sudo apt install -y python3 python3-pip python3-venv nginx git curl

# Install MongoDB (if not already installed). Some newer Ubuntu codenames
# (e.g. 'noble') do not yet have official mongodb-org repo packages. You can
# skip automatic installation by setting SKIP_MONGODB_INSTALL=1 when running
# this script. The script will attempt the official upstream repo for known
# supported codenames and otherwise fall back to the distro package (may be
# an older MongoDB version) or skip if that fails.
if [ "${SKIP_MONGODB_INSTALL:-0}" != "1" ]; then
    echo "🍃 Installing MongoDB..."
    CODENAME=$(lsb_release -cs)
    case "$CODENAME" in
        jammy|focal|buster|bullseye)
            echo "Detected supported codename '$CODENAME' — attempting official MongoDB repo..."
            wget -qO - https://www.mongodb.org/static/pgp/server-7.0.asc | sudo tee /usr/share/keyrings/mongodb-org-7.gpg >/dev/null
            echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-org-7.gpg ] https://repo.mongodb.org/apt/ubuntu $CODENAME/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
            sudo apt update || true
            if sudo apt install -y mongodb-org; then
                sudo systemctl start mongod
                sudo systemctl enable mongod
            else
                echo "⚠️  Failed to install mongodb-org from upstream repo; falling back to distro package..."
                sudo apt update
                if sudo apt install -y mongodb; then
                    echo "Installed distro 'mongodb' package"
                    sudo systemctl start mongodb || sudo systemctl start mongod || true
                    sudo systemctl enable mongodb || true
                else
                    echo "⚠️  Failed to install MongoDB from distro packages. Skipping MongoDB installation. You must provide an external MongoDB and set MONGODB_URI in .env"
                fi
            fi
            ;;
        *)
            echo "⚠️  Ubuntu codename '$CODENAME' not supported by mongodb-org upstream repo in this script."
            echo "    Attempting to install distro 'mongodb' package as a fallback (may be an older version)."
            sudo apt update
            if sudo apt install -y mongodb; then
                sudo systemctl start mongodb || sudo systemctl start mongod || true
                sudo systemctl enable mongodb || true
            else
                echo "⚠️  Failed to install MongoDB via distro packages. Skipping MongoDB installation. You must provide an external MongoDB and set MONGODB_URI in .env"
            fi
            ;;
    esac
else
    echo "ℹ️  SKIP_MONGODB_INSTALL=1 set — skipping MongoDB installation. Make sure MONGODB_URI points to a running MongoDB instance."
fi

# Install PM2 globally (optional)
echo "📱 Installing PM2..."
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g pm2

# Create application directory
echo "📁 Setting up application directory..."
sudo mkdir -p $APP_DIR
sudo mkdir -p $LOG_DIR
sudo chown -R $SERVICE_USER:$SERVICE_USER $APP_DIR
sudo chown -R $SERVICE_USER:$SERVICE_USER $LOG_DIR

# Deploy application files. If the target directory is already a git repository,
# perform a git pull to update it. Otherwise copy the uploaded files.
echo "📋 Deploying application files..."
if [ -d "$APP_DIR/.git" ]; then
    echo "🔄 Detected existing git repo at $APP_DIR — attempting git pull (branch: $BRANCH)"
    # Try to pull as root; if that fails, fall back to copying the uploaded tree
        if git -C "$APP_DIR" pull origin "$BRANCH"; then
        echo "✅ git pull successful"
    else
        echo "⚠️  git pull failed; falling back to copying uploaded files"
        # copy all files including hidden files (.git, dotfiles)
        sudo cp -a /tmp/hardware_exe_api/. $APP_DIR/
    fi
else
    echo "� Fresh install — copying files to $APP_DIR"
    # copy all files including hidden files (.git, dotfiles)
    sudo cp -a /tmp/hardware_exe_api/. $APP_DIR/
fi

# Ensure correct ownership after deploy
sudo chown -R $SERVICE_USER:$SERVICE_USER $APP_DIR

# Set up Python virtual environment
echo "🐍 Setting up Python virtual environment..."
cd $APP_DIR
sudo -u $SERVICE_USER python3 -m venv .venv
sudo -u $SERVICE_USER .venv/bin/pip install --upgrade pip
sudo -u $SERVICE_USER .venv/bin/pip install -r requirements.txt

# Create .env file template if one does not already exist
echo "⚙️ Ensuring environment configuration exists..."
if [ ! -f "$APP_DIR/.env" ]; then
    sudo -u $SERVICE_USER tee $APP_DIR/.env > /dev/null <<'EOF'
# Production environment configuration
PORT=8080
HOST=0.0.0.0
UVICORN_RELOAD=false

# MongoDB configuration (REQUIRED - application will fail without these)
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=fry_external_api

# 1Password secrets (if using 1Password CLI)
# MONGODB_URI=op://vault/item/field
EOF
fi

# Set up systemd service
echo "🔧 Setting up systemd service..."
sudo cp $APP_DIR/deployment/hardware_exe_api.service /etc/systemd/system/hardware_exe_api.service
sudo systemctl daemon-reload
sudo systemctl enable $APP_NAME

# Set up nginx reverse proxy
echo "🌐 Configuring nginx reverse proxy..."
sudo tee /etc/nginx/sites-available/$APP_NAME > /dev/null <<EOF
server {
    listen 80;
    server_name your-domain.com;  # Change this to your actual domain

    location / {
    proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    # Health check endpoint
    location /health {
        access_log off;
        proxy_pass http://127.0.0.1:8080;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/$APP_NAME /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# Start the service
echo "🚀 Starting the service..."
sudo systemctl start $APP_NAME

# Show status
echo "✅ Deployment complete!"
echo ""
echo "Service status:"
sudo systemctl status $APP_NAME --no-pager
echo ""
echo "📝 Next steps:"
echo "1. Edit $APP_DIR/.env with your production configuration"
echo "2. Update nginx server_name in /etc/nginx/sites-available/$APP_NAME"
echo "3. Set up SSL with certbot: sudo apt install certbot python3-certbot-nginx && sudo certbot --nginx"
echo "4. Restart services: sudo systemctl restart $APP_NAME nginx"
echo ""
echo "🔍 Useful commands:"
echo "  View logs: sudo journalctl -u $APP_NAME -f"
echo "  Restart service: sudo systemctl restart $APP_NAME"
echo "  Stop service: sudo systemctl stop $APP_NAME"
echo "  Check status: sudo systemctl status $APP_NAME"
echo ""
echo "📊 PM2 alternative commands:"
echo "  Start with PM2: cd $APP_DIR && pm2 start deployment/ecosystem.config.js"
echo "  View PM2 status: pm2 status"
echo "  View PM2 logs: pm2 logs $APP_NAME"