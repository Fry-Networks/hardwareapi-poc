#!/bin/bash

# Hardware EXE API startup script with secure secret management
set -e

cd /opt/hardware_exe_api

echo "🔐 Hardware EXE API - Starting with secure secrets"
echo ""

# Check if 1Password CLI is available
if ! command -v op &> /dev/null; then
    echo "❌ Error: 1Password CLI (op) is not installed"
    exit 1
fi

echo "Step 1: Loading 1Password service account token..."

token_file="/etc/opt/hardwareapi/op_service_account_token"
if [ ! -f "$token_file" ]; then
    echo "❌ Error: 1Password service account token file not found: $token_file"
    exit 1
fi

# Read token without printing it
OP_SERVICE_ACCOUNT_TOKEN="$(cat "$token_file")"
export OP_SERVICE_ACCOUNT_TOKEN

echo "✅ Service account token loaded"

echo ""
echo "Step 2: Loading .env and resolving any 1Password references..."

# Load .env if present (export all variables)
if [ -f ".env" ]; then
    echo "Parsing .env safely (no direct sourcing)"
    # Read .env line-by-line, ignore comments/empty lines, and export KEY=VALUE
    while IFS= read -r line || [ -n "$line" ]; do
        # Trim leading/trailing whitespace
        line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        # Skip empty lines and comments
        case "$line" in
            "" | \#*) continue ;;
        esac
        # Ensure line contains '='
        if ! echo "$line" | grep -q "=" ; then
            continue
        fi
        key="$(echo "$line" | cut -d'=' -f1 | sed -e 's/[[:space:]]*$//')"
        value="$(echo "$line" | cut -d'=' -f2- | sed -e 's/^[[:space:]]*//')"
        # Strip surrounding single or double quotes
        if [[ "$value" =~ ^\".*\"$ || "$value" =~ ^\'.*\'$ ]]; then
            value="${value:1:$((${#value}-2))}"
        fi
        export "$key"="$value"
    done < .env
    echo "✅ Loaded .env (parsed safely)"
else
    echo "ℹ️ .env not found; continuing with environment and op lookups"
fi

# Helper function to resolve op:// or op/ references using `op read`
resolve_op_var() {
    local key="$1"
    local val="${!key}"
    if [ -z "$val" ]; then
        echo "⚠️  $key is not set in environment or .env"
        return 0
    fi
    if [[ "$val" == op://* || "$val" == op/* ]]; then
        if resolved=$(op read "$val" 2>/dev/null); then
            export "$key"="$resolved"
            echo "✅ $key resolved from 1Password ($val)"
        else
            echo "❌ Failed to retrieve $key from 1Password ($val)"
            echo "   Please ensure you're signed in and that the op path is correct"
            exit 1
        fi
    else
        echo "ℹ️ $key set from .env or environment"
    fi
}

# Resolve the specific secrets the app uses (adjust list if you add more)
resolve_op_var MONGODB_URI
resolve_op_var API_BEARER_TOKEN
resolve_op_var API_BEARER_TOKEN_FLXTIME
resolve_op_var API_BEARER_TOKEN_ADMIN
resolve_op_var API_BEARER_TOKEN_DROPWIRELESS

echo ""
echo "Step 3: Setting up environment..."

# Export environment variables
export PORT=8081
export HOST=127.0.0.1
export UVICORN_RELOAD=false
export MONGODB_URI="$MONGODB_URI"
export API_BEARER_TOKEN="$API_BEARER_TOKEN"
export API_BEARER_TOKEN_FLXTIME="$API_BEARER_TOKEN_FLXTIME"
export API_BEARER_TOKEN_ADMIN="$API_BEARER_TOKEN_ADMIN"
export API_BEARER_TOKEN_DROPWIRELESS="$API_BEARER_TOKEN_DROPWIRELESS"

echo "✅ Environment variables configured"
echo "   - Port: $PORT"
echo "   - Host: $HOST"
echo "   - MongoDB: Connected to 1Password secret"

echo ""
echo "Step 4: Starting Hardware EXE API..."
echo "🚀 API will be available at: http://127.0.0.1:8081"
echo "📝 Press Ctrl+C to stop"
echo ""

# Start the application
.venv/bin/python app.py
