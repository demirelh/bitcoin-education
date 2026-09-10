#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
caddyfile="/etc/caddy/Caddyfile"
snippet="${repo_dir}/deploy/Caddyfile.almanya24-dev"
unit="${repo_dir}/deploy/almanya24-dev-preview.service"
staged="$(mktemp)"
cleaned="$(mktemp)"
trap 'rm -f "${staged}" "${cleaned}"' EXIT

awk '
  /# BEGIN ALMANYA24 DEV PREVIEW/ { skip = 1; next }
  /# END ALMANYA24 DEV PREVIEW/ { skip = 0; next }
  !skip { print }
' "${caddyfile}" >"${cleaned}"

awk -v snippet="${snippet}" '
  /catch-all/ && !inserted {
    while ((getline line < snippet) > 0) print line
    close(snippet)
    inserted = 1
  }
  { print }
  END {
    if (!inserted) {
      print "Homepage catch-all marker not found" >"/dev/stderr"
      exit 1
    }
  }
' "${cleaned}" >"${staged}"

validation_hash="$(caddy hash-password --algorithm bcrypt --plaintext validation-only)"
SAHIMI_BASIC_AUTH_USER=validation-user \
  SAHIMI_BASIC_AUTH_HASH="${validation_hash}" \
  caddy validate --config "${staged}" --adapter caddyfile

while read -r pid; do
  [[ -n "${pid}" ]] || continue
  command_line="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
  if [[ "${command_line}" == *"data/almanya24-preview/serve_preview.py --port 8765"* ]]; then
    kill "${pid}"
  else
    echo "Port 8765 is occupied by an unexpected process: ${command_line}" >&2
    exit 1
  fi
done < <(fuser 8765/tcp 2>/dev/null | tr ' ' '\n')

for _ in {1..20}; do
  if ! fuser -s 8765/tcp; then
    break
  fi
  sleep 0.25
done
if fuser -s 8765/tcp; then
  echo "The previous preview process did not release port 8765." >&2
  exit 1
fi

install -m 0644 "${unit}" /etc/systemd/system/almanya24-dev-preview.service
systemctl daemon-reload
systemctl enable --now almanya24-dev-preview.service
curl --fail --silent --show-error http://127.0.0.1:8765/ >/dev/null

backup="${caddyfile}.bak-almanya24-dev-$(date -u +%Y%m%dT%H%M%SZ)"
cp --preserve=mode,ownership,timestamps "${caddyfile}" "${backup}"
install -o root -g root -m 0644 "${staged}" "${caddyfile}"

rollback_caddy() {
  cp --preserve=mode,ownership,timestamps "${backup}" "${caddyfile}"
  systemctl reload caddy || true
}
trap rollback_caddy ERR

systemctl reload caddy

status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  https://sahimi.app/almanya24-dev/)"
if [[ "${status}" != "401" ]]; then
  echo "Expected protected preview to return HTTP 401, got ${status}." >&2
  exit 1
fi
trap - ERR
echo "Protected ALMANYA24 DEV is available at https://sahimi.app/almanya24-dev/"
echo "Caddy backup: ${backup}"
