#!/usr/bin/env bash
# Private PKI for the Jetson's HTTPS endpoint.
#
# The FishAI Android app trusts ONLY the CA made here (its certificate is compiled
# into the app as mobile/android/app/jetson-ca.pem) and requires the server
# certificate to carry the name "fishai-jetson". The Jetson's IP comes from DHCP and
# changes, so the certificate names the role, not an address.
#
#   scripts/jetson-tls.sh ca              # once: create the CA (keep ca.key OFFLINE)
#   scripts/jetson-tls.sh leaf <jetson>   # per Jetson: key + cert signed by the CA
#
# Output goes to $JETSON_TLS_DIR (default ./certs, git-ignored). Copy to a Jetson only
# <jetson>.key and <jetson>.chain.pem — never ca.key. Whoever holds ca.key can mint a
# certificate every FishAI app will accept as a genuine Jetson.
set -euo pipefail

DIR="${JETSON_TLS_DIR:-./certs}"
SERVER_NAME="fishai-jetson"   # must match JetsonTls.SERVER_NAME in the Android app
CA_DAYS=3650                  # 10 years
LEAF_DAYS=1825                # 5 years — renew before expiry or the app stops connecting

if ! openssl version | grep -q '^OpenSSL 3'; then
  echo "error: needs OpenSSL 3.x (macOS: brew install openssl; /usr/bin/openssl is LibreSSL)" >&2
  exit 1
fi

umask 077
mkdir -p "$DIR"

case "${1:-}" in
  ca)
    if [[ -e "$DIR/ca.key" ]]; then
      echo "error: $DIR/ca.key already exists. Replacing the CA invalidates every Jetson" >&2
      echo "certificate and requires a new app build; delete it by hand if you mean it." >&2
      exit 1
    fi
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
      -keyout "$DIR/ca.key" -out "$DIR/ca.crt" -days "$CA_DAYS" \
      -subj "/O=FishAI/CN=FishAI Jetson CA" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
      -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -addext "subjectKeyIdentifier=hash"
    chmod 644 "$DIR/ca.crt"
    echo "CA created: $DIR/ca.crt (public — goes into the app), $DIR/ca.key (SECRET — store offline)"
    ;;
  leaf)
    name="${2:?usage: $0 leaf <jetson-name>}"
    [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "error: name may contain only A-Z a-z 0-9 . _ -" >&2; exit 1; }
    [[ -e "$DIR/ca.key" ]] || { echo "error: no CA in $DIR — run '$0 ca' first" >&2; exit 1; }
    ext="$(mktemp)"; trap 'rm -f "$ext" "$DIR/$name.csr"' EXIT
    cat > "$ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=DNS:$SERVER_NAME
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
EOF
    openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
      -keyout "$DIR/$name.key" -out "$DIR/$name.csr" \
      -subj "/O=FishAI/OU=$name/CN=$SERVER_NAME"
    openssl x509 -req -in "$DIR/$name.csr" -CA "$DIR/ca.crt" -CAkey "$DIR/ca.key" \
      -set_serial "0x$(openssl rand -hex 16)" -days "$LEAF_DAYS" -sha256 \
      -extfile "$ext" -out "$DIR/$name.crt"
    cat "$DIR/$name.crt" "$DIR/ca.crt" > "$DIR/$name.chain.pem"
    chmod 644 "$DIR/$name.crt" "$DIR/$name.chain.pem"
    openssl verify -CAfile "$DIR/ca.crt" "$DIR/$name.crt"
    echo "Jetson cert: copy $DIR/$name.key and $DIR/$name.chain.pem to the Jetson (see DEPLOYMENT.md)"
    ;;
  *)
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
