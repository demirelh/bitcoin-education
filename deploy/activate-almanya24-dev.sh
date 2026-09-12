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
daily_unit="${repo_dir}/deploy/almanya24-daily.service"
daily_timer="${repo_dir}/deploy/almanya24-daily.timer"
staged="$(mktemp)"
cleaned="$(mktemp)"
netrc=""
trap 'rm -f "${staged}" "${cleaned}" "${netrc}"' EXIT

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

homepage_before="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  https://sahimi.app/)"
dashboard_before="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  https://sahimi.app/sahimi/)"

if systemctl cat almanya24-dev-preview.service >/dev/null 2>&1; then
  systemctl stop almanya24-dev-preview.service
fi

while read -r pid; do
  [[ -n "${pid}" ]] || continue
  command_line="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
  process_cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
  process_user="$(stat -c '%U' "/proc/${pid}" 2>/dev/null || true)"
  if [[ "${process_cwd}" != "${repo_dir}" \
    || "${process_user}" != "pi" \
    || "${command_line}" != *"scripts/almanya24_preview/serve_preview.py --port 8765"* ]]; then
    echo "Port 8765 is occupied by an unexpected process: ${command_line}" >&2
    exit 1
  fi
  kill "${pid}"
  for _ in {1..40}; do
    kill -0 "${pid}" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "${pid}" 2>/dev/null; then
    current_command="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
    current_cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
    if [[ "${current_cwd}" != "${repo_dir}" \
      || "${current_command}" != *"scripts/almanya24_preview/serve_preview.py --port 8765"* ]]; then
      echo "Preview PID ${pid} changed identity while stopping; refusing SIGKILL." >&2
      exit 1
    fi
    kill -9 "${pid}"
  fi
done < <(fuser 8765/tcp 2>/dev/null | tr ' ' '\n')

for _ in {1..40}; do
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

# The protected Caddy route is installed before the preview is started, so a
# failure at any later step can never leave preview content reachable through
# the previous unauthenticated route.
backup="${caddyfile}.bak-almanya24-dev-$(date -u +%Y%m%dT%H%M%SZ)"
cp --preserve=mode,ownership,timestamps "${caddyfile}" "${backup}"

rollback_activation() {
  systemctl stop almanya24-dev-preview.service 2>/dev/null || true
  systemctl disable almanya24-dev-preview.service 2>/dev/null || true
  cp --preserve=mode,ownership,timestamps "${backup}" "${caddyfile}"
  systemctl reload caddy || true
  echo "Activation failed; preview stopped and Caddy restored from ${backup}." >&2
}
trap rollback_activation ERR

install -o root -g root -m 0644 "${staged}" "${caddyfile}"
systemctl reload caddy
systemctl is-active --quiet caddy

# basic_auth is evaluated ahead of the reverse proxy, so these must already
# answer 401 while the preview backend is still down.
for path in "/" "/almanya/example-article/" "/assets/site.css"; do
  status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    "https://sahimi.app/almanya24-dev${path}")"
  if [[ "${status}" != "401" ]]; then
    echo "Expected anonymous ${path} request to return HTTP 401, got ${status}." >&2
    exit 1
  fi
done

# Type=simple reports the unit as started right after fork, roughly 250 ms
# before Python has bound the socket, so readiness is polled instead of assumed.
wait_for_preview_ready() {
  local deadline=$((SECONDS + 45))
  local status="000"
  local state=""
  while ((SECONDS < deadline)); do
    state="$(systemctl show almanya24-dev-preview.service -p ActiveState --value)"
    if [[ "${state}" != "active" && "${state}" != "activating" ]]; then
      echo "Preview service left startup with ActiveState '${state}'." >&2
      journalctl -u almanya24-dev-preview.service -n 30 --no-pager >&2 || true
      return 1
    fi
    status="$(curl --silent --max-time 5 --output /dev/null \
      --write-out '%{http_code}' http://127.0.0.1:8765/ || true)"
    if [[ "${status}" == "200" ]]; then
      return 0
    fi
    sleep 0.5
  done
  echo "Preview did not serve HTTP 200 on 127.0.0.1:8765 within 45s" \
    "(last response code: ${status})." >&2
  journalctl -u almanya24-dev-preview.service -n 30 --no-pager >&2 || true
  return 1
}

systemctl enable --now almanya24-dev-preview.service
wait_for_preview_ready

main_pid="$(systemctl show almanya24-dev-preview.service -p MainPID --value)"
mapfile -t port_pids < <(fuser 8765/tcp 2>/dev/null | tr ' ' '\n' | sed '/^$/d')
if [[ "${#port_pids[@]}" -ne 1 || "${port_pids[0]}" != "${main_pid}" ]]; then
  echo "Expected exactly systemd MainPID ${main_pid} on port 8765; got: ${port_pids[*]:-none}" >&2
  exit 1
fi

# Re-checked with the backend live: anonymous requests must still be rejected
# now that the preview can actually serve content.
for path in "/" "/almanya/example-article/" "/assets/site.css"; do
  status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    "https://sahimi.app/almanya24-dev${path}")"
  if [[ "${status}" != "401" ]]; then
    echo "Expected anonymous ${path} request to return HTTP 401, got ${status}." >&2
    exit 1
  fi
done

netrc="$(mktemp)"
chmod 0600 "${netrc}"
printf 'machine sahimi.app login preview-verify password invalid-%s\n' \
  "${RANDOM}${RANDOM}" >"${netrc}"

# Deliberately wrong credentials must also be rejected. This distinguishes an
# enforced basic_auth from a route that merely happens to answer 401.
for path in "/" "/assets/site.css"; do
  status="$(curl --netrc-file "${netrc}" --silent --output /dev/null \
    --write-out '%{http_code}' "https://sahimi.app/almanya24-dev${path}")"
  if [[ "${status}" != "401" ]]; then
    echo "Expected wrong-credential ${path} request to return HTTP 401, got ${status}." >&2
    exit 1
  fi
done

if [[ "${PREVIEW_VERIFY_AUTH:-1}" == "1" ]]; then
  read -r -p "Preview Basic Auth username: " verify_user
  read -r -s -p "Preview Basic Auth password: " verify_password
  echo
  printf 'machine sahimi.app login %s password %s\n' \
    "${verify_user}" "${verify_password}" >"${netrc}"
  unset verify_password

  for path in "/" "/assets/site.css"; do
    status="$(curl --netrc-file "${netrc}" --silent --output /dev/null \
      --write-out '%{http_code}' "https://sahimi.app/almanya24-dev${path}")"
    if [[ "${status}" != "200" ]]; then
      echo "Expected authenticated ${path} request to return HTTP 200, got ${status}." >&2
      exit 1
    fi
  done
else
  echo "NOTE: authenticated check skipped (PREVIEW_VERIFY_AUTH=0)." >&2
  echo "      The route reuses SAHIMI_BASIC_AUTH_USER/HASH from" >&2
  echo "      /etc/caddy/sahimi-auth.env, i.e. the same credentials as /sahimi/." >&2
fi

homepage_after="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  https://sahimi.app/)"
dashboard_after="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  https://sahimi.app/sahimi/)"
if [[ "${homepage_after}" != "${homepage_before}" \
  || "${dashboard_after}" != "${dashboard_before}" ]]; then
  echo "Existing Sahimi routes changed status during activation." >&2
  exit 1
fi
# The daily transcript-to-news timer. Installed last, and only once the
# protected route has been proven to reject anonymous requests: the timer
# publishes drafts onto exactly that route, so activating it before the
# protection is verified would be the one ordering that could expose them.
install -m 0644 "${daily_unit}" /etc/systemd/system/almanya24-daily.service
install -m 0644 "${daily_timer}" /etc/systemd/system/almanya24-daily.timer
systemctl daemon-reload
systemctl enable --now almanya24-daily.timer

if ! systemctl is-active --quiet almanya24-daily.timer; then
  echo "almanya24-daily.timer did not come up." >&2
  systemctl status almanya24-daily.timer --no-pager >&2 || true
  exit 1
fi

# A budget that is silently non-zero would be the one activation mistake that
# costs money, so it is reported rather than assumed.
#
# `systemctl show -p Environment` lists only the unit's own Environment=
# directives; values from EnvironmentFile= are resolved at start and never
# appear there. Reading only that reported 0 USD while the effective budget was
# the approved one -- the most misleading answer this script could give.
budget_file="${repo_dir}/data/almanya24-preview/daily-budget.env"
daily_budget="$(systemctl show almanya24-daily.service \
  -p Environment --value | tr ' ' '\n' | grep '^ALMANYA24_DAILY_BUDGET_USD=' \
  | cut -d= -f2 || true)"
budget_source="unit default"
if [[ -f "${budget_file}" ]]; then
  file_budget="$(grep -E '^ALMANYA24_DAILY_BUDGET_USD=' "${budget_file}" \
    | tail -n1 | cut -d= -f2 || true)"
  if [[ -n "${file_budget}" ]]; then
    daily_budget="${file_budget}"
    budget_source="daily-budget.env"
  fi
fi
echo "Daily run budget: ${daily_budget:-unknown} USD from ${budget_source} (0 = paid calls locked)."

trap - ERR
echo "Protected ALMANYA24 DEV is available at https://sahimi.app/almanya24-dev/"
echo "Operations status: https://sahimi.app/almanya24-dev/_durum/"
echo "Caddy backup: ${backup}"
