#!/bin/sh
set -eu

token_file="/run/secrets/op_service_account_token"
if [ ! -f "$token_file" ]; then
  echo "Missing 1Password service account token file: $token_file" >&2
  exit 1
fi

OP_SERVICE_ACCOUNT_TOKEN="$(cat "$token_file")"
export OP_SERVICE_ACCOUNT_TOKEN

# Resolve all op:// references once at startup and cache as plain env vars.
# This avoids repeated 1Password API calls on every container restart.
resolve_op_ref() {
  var_name="$1"
  eval var_val="\${${var_name}:-}"
  case "$var_val" in
    op://*)
      resolved="$(op read "$var_val")" || {
        echo "Failed to resolve $var_name from 1Password" >&2
        exit 1
      }
      export "$var_name=$resolved"
      ;;
  esac
}

resolve_op_ref MONGODB_URI
resolve_op_ref API_BEARER_TOKEN
resolve_op_ref API_BEARER_TOKEN_FLXTIME
resolve_op_ref API_BEARER_TOKEN_ADMIN
resolve_op_ref API_BEARER_TOKEN_DROPWIRELESS

# Done with 1Password — clear the service account token from the environment
unset OP_SERVICE_ACCOUNT_TOKEN

if [ "$(id -u)" = "0" ]; then
  ca_src="${MONGO_CA_CERT_PATH:-/etc/ssl/mongo/mongo-ca.crt}"
  if [ -f "$ca_src" ]; then
    ca_dst="/tmp/mongo-ca.crt"
    cp "$ca_src" "$ca_dst"
    chmod 0444 "$ca_dst"
    if printf "%s" "$MONGODB_URI" | grep -q "tlsCAFile=${ca_src}"; then
      MONGODB_URI="$(printf "%s" "$MONGODB_URI" | sed "s|tlsCAFile=${ca_src}|tlsCAFile=${ca_dst}|")"
      export MONGODB_URI
    fi
  fi

  exec gosu app "$@"
fi

exec "$@"
