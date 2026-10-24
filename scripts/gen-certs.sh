#!/usr/bin/env bash
# Generate TLS material for the test postgres into .rowx-certs/:
#   ca.crt / ca.key            the trusted CA
#   server.crt / server.key    valid for localhost and 127.0.0.1, signed by the CA
#   wrong-host.crt / .key      signed by the CA but valid only for wrong-host.example
#   other-ca.crt               an unrelated CA, for the wrong-CA case
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${1:-$root/.rowx-certs}"
mkdir -p "$out"
cd "$out"

new_ca() { # name
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=rowx test $1" \
    -keyout "$1.key" -out "$1.crt" 2>/dev/null
}

issue() { # name, subjectAltName
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$1" -keyout "$1.key" -out "$1.csr" 2>/dev/null
  printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\n' "$2" >"$1.ext"
  openssl x509 -req -in "$1.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -days 30 \
    -extfile "$1.ext" -out "$1.crt" 2>/dev/null
  rm -f "$1.csr" "$1.ext"
}

new_ca ca
new_ca other-ca
rm -f other-ca.key
issue server "DNS:localhost,IP:127.0.0.1"
issue wrong-host "DNS:wrong-host.example"
chmod 644 ./*.crt
chmod 600 ./*.key
