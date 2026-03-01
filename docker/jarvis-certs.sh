#!/bin/bash
# jarvis-certs.sh — Generate CA, server certs, client certs, and user tokens
#
# Usage:
#   ./jarvis-certs.sh [hostname] [output-dir]       # Server mode (CA + server cert + token)
#   ./jarvis-certs.sh --client <username> [output-dir]  # Client cert mode
#
# Server mode generates:
#   ca.key, ca.crt           — CA (10-year validity)
#   server.key, server.crt   — Server cert signed by CA (1-year, with SANs)
#   + prints a Bearer token for backward compat
#
# Client mode generates:
#   <username>.key, <username>.crt  — Client cert signed by CA (1-year, clientAuth)
#   Requires ca.key and ca.crt in the output directory.
#
# Examples:
#   ./jarvis-certs.sh jarvis.local ./certs
#   ./jarvis-certs.sh --client raph ./certs

set -euo pipefail

# --- Client cert mode ---
if [ "${1:-}" = "--client" ]; then
    USERNAME="${2:-}"
    OUTDIR="${3:-./certs}"

    if [ -z "$USERNAME" ]; then
        echo "Usage: $0 --client <username> [output-dir]" >&2
        exit 1
    fi

    # Validate username: lowercase alphanumeric + dots/underscores/hyphens, 1-64 chars
    if ! echo "$USERNAME" | grep -qE '^[a-z0-9._-]{1,64}$'; then
        echo "ERROR: Username must match ^[a-z0-9._-]{1,64}$" >&2
        echo "  Got: '$USERNAME'" >&2
        exit 1
    fi

    # Require CA files
    if [ ! -f "$OUTDIR/ca.key" ] || [ ! -f "$OUTDIR/ca.crt" ]; then
        echo "ERROR: CA files not found in $OUTDIR" >&2
        echo "  Run '$0 <hostname> $OUTDIR' first, or copy ca.key + ca.crt there." >&2
        exit 1
    fi

    echo "=== Generating client certificate for '$USERNAME' ==="

    # Generate client key + CSR
    openssl genrsa -out "$OUTDIR/${USERNAME}.key" 2048 2>/dev/null
    openssl req -new -key "$OUTDIR/${USERNAME}.key" \
        -out "$OUTDIR/${USERNAME}.csr" \
        -subj "/CN=$USERNAME" 2>/dev/null

    # Sign with CA (clientAuth only — cannot be used as server cert)
    cat > "$OUTDIR/${USERNAME}.ext" <<EXTEOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature
extendedKeyUsage = clientAuth
EXTEOF

    openssl x509 -req -in "$OUTDIR/${USERNAME}.csr" \
        -CA "$OUTDIR/ca.crt" -CAkey "$OUTDIR/ca.key" -CAcreateserial \
        -out "$OUTDIR/${USERNAME}.crt" -days 365 \
        -extfile "$OUTDIR/${USERNAME}.ext" 2>/dev/null

    rm -f "$OUTDIR/${USERNAME}.csr" "$OUTDIR/${USERNAME}.ext" "$OUTDIR/ca.srl"
    chmod 600 "$OUTDIR/${USERNAME}.key"

    echo "[OK] Client cert generated for '$USERNAME' (1-year validity)"
    echo
    echo "Configure Claude Code:"
    echo "  export CLAUDE_CODE_CLIENT_CERT=$OUTDIR/${USERNAME}.crt"
    echo "  export CLAUDE_CODE_CLIENT_KEY=$OUTDIR/${USERNAME}.key"
    echo "  export NODE_EXTRA_CA_CERTS=$OUTDIR/ca.crt"
    echo
    echo "=== Done ==="
    exit 0
fi

# --- Server cert mode ---
HOSTNAME="${1:-localhost}"
OUTDIR="${2:-./certs}"

mkdir -p "$OUTDIR"

echo "=== Generating Jarvis CA and server certificates ==="
echo "  Hostname: $HOSTNAME"
echo "  Output:   $OUTDIR"
echo

# --- CA (10-year) ---
if [ ! -f "$OUTDIR/ca.key" ]; then
    openssl genrsa -out "$OUTDIR/ca.key" 4096 2>/dev/null
    openssl req -new -x509 -key "$OUTDIR/ca.key" \
        -out "$OUTDIR/ca.crt" -days 3650 \
        -subj "/CN=Jarvis CA" 2>/dev/null
    echo "[OK] CA generated (10-year validity)"
else
    echo "[OK] CA already exists, reusing"
fi

# --- Server cert (1-year, signed by CA) ---
# Build SAN extension: always include localhost + the requested hostname
SAN="DNS:localhost,DNS:$HOSTNAME,IP:127.0.0.1"

openssl genrsa -out "$OUTDIR/server.key" 2048 2>/dev/null
openssl req -new -key "$OUTDIR/server.key" \
    -out "$OUTDIR/server.csr" \
    -subj "/CN=$HOSTNAME" 2>/dev/null

cat > "$OUTDIR/server.ext" <<EXTEOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = $SAN
EXTEOF

openssl x509 -req -in "$OUTDIR/server.csr" \
    -CA "$OUTDIR/ca.crt" -CAkey "$OUTDIR/ca.key" -CAcreateserial \
    -out "$OUTDIR/server.crt" -days 365 \
    -extfile "$OUTDIR/server.ext" 2>/dev/null

rm -f "$OUTDIR/server.csr" "$OUTDIR/server.ext" "$OUTDIR/ca.srl"
echo "[OK] Server cert generated (1-year validity, SANs: $SAN)"

# --- Generate a user token (backward compat) ---
echo
echo "=== User Token (for Bearer auth fallback) ==="
TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
HASH=$(python3 -c "import hashlib; print(hashlib.sha256('$TOKEN'.encode()).hexdigest())")

echo
echo "Raw token (save securely — not recoverable from config):"
echo "  $TOKEN"
echo
echo "Add this to your ~/.jarvis/config.json under server.auth:"
echo
cat <<CONFIGEOF
{
  "server": {
    "auth": {
      "enabled": true,
      "tokens": {
        "$HASH": "your-username"
      }
    }
  }
}
CONFIGEOF

echo
echo "For mTLS (recommended), generate a client cert instead:"
echo "  $0 --client <username> $OUTDIR"
echo
echo "=== Done ==="
