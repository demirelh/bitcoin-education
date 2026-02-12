#!/usr/bin/env bash
# btcedu web dashboard - production deployment script
# Run from: /home/pi/AI-Startup-Lab/bitcoin-education
set -euo pipefail

PROJ="/home/pi/AI-Startup-Lab/bitcoin-education"
VENV="$PROJ/.venv"

echo "=== btcedu web dashboard deployment ==="
echo ""

# 1. Install gunicorn
echo "[1/4] Installing gunicorn..."
"$VENV/bin/pip" install -q 'gunicorn>=22.0.0'
echo "  OK: $(\"$VENV/bin/gunicorn\" --version)"

# 2. Install systemd unit
echo "[2/4] Installing systemd service..."
sudo cp "$PROJ/deploy/btcedu-web.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable btcedu-web.service
echo "  OK: btcedu-web.service enabled"

# 3. Generate basic auth password
echo "[3/4] Setting up basic auth..."
echo "  Enter a password for the dashboard (user: pi):"
read -r -s -p "  Password: " PASSWORD
echo ""
HASH=$(caddy hash-password --plaintext "$PASSWORD")
echo "  Hash generated. Update /etc/caddy/Caddyfile manually:"
echo ""
echo "  Add this block INSIDE lnodebtc.duckdns.org { } BEFORE existing handle blocks:"
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
echo "[4/4] Starting btcedu-web service..."
sudo systemctl start btcedu-web.service
echo "  OK: btcedu-web started"
echo ""
echo "  After updating Caddyfile, reload caddy:"
echo "    sudo systemctl reload caddy"
echo ""
echo "  Dashboard will be at: https://lnodebtc.duckdns.org/dashboard/"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status btcedu-web"
echo "    sudo journalctl -u btcedu-web -f"
echo "    sudo systemctl restart btcedu-web"
