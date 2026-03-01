#!/usr/bin/env bash
# btcedu production deployment setup
# Installs systemd services (web + pipeline timers) and configures Caddy reverse proxy.
#
# Run from: /home/pi/AI-Startup-Lab/bitcoin-education
# Prerequisites: .venv exists, .env is configured, pip install -e ".[web]" done
set -euo pipefail

PROJ="/home/pi/AI-Startup-Lab/bitcoin-education"
VENV="$PROJ/.venv"
DEPLOY="$PROJ/deploy"

echo "=== btcedu production setup ==="
echo ""

# 0. Preflight checks
echo "[0/5] Preflight checks..."
if [ ! -d "$VENV" ]; then
    echo "  ERROR: Virtual environment not found at $VENV"
    echo "  Run: python -m venv .venv && .venv/bin/pip install -e '.[web]'"
    exit 1
fi
if [ ! -f "$PROJ/.env" ]; then
    echo "  WARNING: .env not found — systemd services use EnvironmentFile=$PROJ/.env"
fi
# Verify gunicorn is installed (part of [web] extras)
if ! "$VENV/bin/python" -c "import gunicorn" 2>/dev/null; then
    echo "  Installing web dependencies..."
    "$VENV/bin/pip" install -q -e ".[web]"
fi
echo "  OK: gunicorn $("$VENV/bin/gunicorn" --version 2>&1 | head -1)"

# 1. Install systemd services
echo "[1/5] Installing systemd services..."
UNITS=(
    btcedu-web.service
    btcedu-detect.service
    btcedu-detect.timer
    btcedu-run.service
    btcedu-run.timer
)
for unit in "${UNITS[@]}"; do
    sudo cp "$DEPLOY/$unit" /etc/systemd/system/
    echo "  Installed: $unit"
done
sudo systemctl daemon-reload
echo "  OK: systemd reloaded"

# 2. Enable services and timers
echo "[2/5] Enabling services and timers..."
sudo systemctl enable btcedu-web.service
sudo systemctl enable btcedu-detect.timer
sudo systemctl enable btcedu-run.timer
echo "  OK: btcedu-web.service, btcedu-detect.timer, btcedu-run.timer enabled"

# 3. Generate basic auth password for Caddy
echo "[3/5] Setting up basic auth..."
echo "  Enter a password for the dashboard (user: pi):"
read -r -s -p "  Password: " PASSWORD
echo ""
HASH=$(caddy hash-password --plaintext "$PASSWORD")
echo "  Hash generated."
echo ""
echo "  Update /etc/caddy/Caddyfile — add this block INSIDE lnodebtc.duckdns.org { }:"
echo "  (or copy from deploy/Caddyfile.dashboard and replace HASH_HERE)"
echo ""
echo "    redir /dashboard /dashboard/ permanent"
echo ""
echo "    @dashboard path /dashboard/*"
echo "    handle @dashboard {"
echo "        uri strip_prefix /dashboard"
echo "        basicauth {"
echo "            pi $HASH"
echo "        }"
echo "        reverse_proxy 127.0.0.1:8091"
echo "    }"
echo ""
echo "    header {"
echo "        X-Content-Type-Options nosniff"
echo "        X-Frame-Options DENY"
echo "        Referrer-Policy no-referrer"
echo "    }"
echo ""

# 4. Start services
echo "[4/5] Starting services..."
sudo systemctl start btcedu-web.service
sudo systemctl start btcedu-detect.timer
sudo systemctl start btcedu-run.timer
echo "  OK: all services started"

# 5. Verify
echo "[5/5] Verifying..."
echo ""
echo "  Services:"
for svc in btcedu-web.service btcedu-detect.timer btcedu-run.timer; do
    STATUS=$(systemctl is-active "$svc" 2>/dev/null || echo "inactive")
    echo "    $svc: $STATUS"
done
echo ""
echo "  Timer schedules:"
echo "    btcedu-detect.timer: every 6 hours"
echo "    btcedu-run.timer:    daily at 02:00"
echo ""
echo "  After updating Caddyfile, reload caddy:"
echo "    sudo systemctl reload caddy"
echo ""
echo "  Dashboard: https://lnodebtc.duckdns.org/dashboard/"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status btcedu-web"
echo "    sudo journalctl -u btcedu-web -f"
echo "    sudo systemctl list-timers btcedu-*"
echo "    sudo systemctl restart btcedu-web"
