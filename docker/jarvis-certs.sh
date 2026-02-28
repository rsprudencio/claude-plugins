#!/bin/bash
# jarvis-certs.sh — Generate self-signed CA, server cert, and user tokens
#
# Usage:
#   ./jarvis-certs.sh [hostname] [output-dir]
#
# Generates:
#   ca.key, ca.crt           — CA (10-year validity)
#   server.key, server.crt   — Server cert signed by CA (1-year, with SANs)
#
# Also generates a user token and prints the config.json snippet to paste.
#
# Example:
#   ./jarvis-certs.sh jarvis.local ./certs
#   # Copy the printed tokens block into ~/.jarvis/config.json

set -euo pipefail

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
subjectAltName = $SAN
EXTEOF

openssl x509 -req -in "$OUTDIR/server.csr" \
    -CA "$OUTDIR/ca.crt" -CAkey "$OUTDIR/ca.key" -CAcreateserial \
    -out "$OUTDIR/server.crt" -days 365 \
    -extfile "$OUTDIR/server.ext" 2>/dev/null

rm -f "$OUTDIR/server.csr" "$OUTDIR/server.ext" "$OUTDIR/ca.srl"
echo "[OK] Server cert generated (1-year validity, SANs: $SAN)"

# --- Generate a user token ---
echo
echo "=== User Token ==="
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
echo "Then configure Claude Code's .mcp.json with:"
echo '  "headers": { "Authorization": "Bearer '"$TOKEN"'" }'
echo
echo "=== Done ==="
