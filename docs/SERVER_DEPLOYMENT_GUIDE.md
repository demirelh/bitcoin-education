# Server Deployment Guide

Complete instructions for deploying the btcedu application on the production server (lnodebtc.duckdns.org).

## Overview

The deployment consists of three systemd services:

| Service | Type | Schedule | Purpose |
|---------|------|----------|---------|
| `btcedu-web.service` | Long-running | Always on | Web dashboard (gunicorn on port 8091) |
| `btcedu-detect.timer` | Periodic | Every 6 hours | Scan RSS feed for new episodes |
| `btcedu-run.timer` | Periodic | Daily at 02:00 | Process latest pending episode |

Deployments are automated via GitHub Actions: push to `main` triggers SSH into the server and runs `run.sh`.

---

## Initial Setup

### 1. Prerequisites

```bash
cd /home/pi/AI-Startup-Lab/bitcoin-education

# Virtual environment
python -m venv .venv
.venv/bin/pip install -e ".[web]"

# YouTube publishing (optional, if data/client_secret.json exists)
.venv/bin/pip install -e ".[youtube]"

# Environment file (required — systemd units load it via EnvironmentFile)
cp .env.example .env
# Edit .env with API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)
chmod 600 .env

# Database
.venv/bin/btcedu init-db
.venv/bin/btcedu migrate

# ffmpeg (required for video rendering)
sudo apt install ffmpeg
```

### 2. Install Services

**Automated (recommended):**

```bash
./deploy/setup-web.sh
```

This installs all 5 systemd units, enables them, configures Caddy basic auth, and starts everything.

**Manual:**

```bash
sudo cp deploy/btcedu-web.service /etc/systemd/system/
sudo cp deploy/btcedu-detect.service deploy/btcedu-detect.timer /etc/systemd/system/
sudo cp deploy/btcedu-run.service deploy/btcedu-run.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btcedu-web.service
sudo systemctl enable --now btcedu-detect.timer
sudo systemctl enable --now btcedu-run.timer
```

### 3. Configure Sudo for Automated Deployments

`run.sh` needs passwordless sudo for service restarts:

```bash
echo "pi ALL=(ALL) NOPASSWD: /bin/systemctl restart btcedu-web, /bin/systemctl status btcedu-web, /bin/systemctl stop btcedu-web, /bin/systemctl start btcedu-web, /bin/systemctl is-active btcedu-web, /bin/systemctl daemon-reload, /bin/systemctl restart btcedu-detect.timer, /bin/systemctl restart btcedu-run.timer" | sudo tee /etc/sudoers.d/pi-btcedu
sudo chmod 0440 /etc/sudoers.d/pi-btcedu
sudo visudo -c
```

Test: `sudo -n systemctl restart btcedu-web` should run without a password prompt.

### 4. Configure Public Access (Optional)

To expose the dashboard via HTTPS through Caddy:

1. Generate a password hash: `caddy hash-password --plaintext 'YOUR_PASSWORD'`
2. Edit `/etc/caddy/Caddyfile` — add the block from [deploy/Caddyfile.dashboard](../deploy/Caddyfile.dashboard), replacing `HASH_HERE`
3. Reload: `sudo systemctl reload caddy`
4. Dashboard at: https://lnodebtc.duckdns.org/dashboard/

---

## Automated Deployment Flow

```
Push to main
    |
GitHub Actions (.github/workflows/deploy.yml)
    |
SSH to pi@lnodebtc.duckdns.org
    |
run.sh
    |- git pull origin main
    |- pip install -e ".[web]" (+ .[youtube] if configured)
    |- btcedu init-db && btcedu migrate
    |- systemctl daemon-reload
    |- systemctl restart btcedu-web
    |- systemctl restart btcedu-detect.timer
    +- systemctl restart btcedu-run.timer
```

---

## Service Details

### btcedu-web.service

```ini
[Service]
Type=notify
User=pi
WorkingDirectory=/home/pi/AI-Startup-Lab/bitcoin-education
ExecStart=.venv/bin/gunicorn -w 1 --threads 4 -b 127.0.0.1:8091 --timeout 300 "btcedu.web.app:create_app()"
EnvironmentFile=.env
Restart=on-failure
RestartSec=5
```

- Binds to `127.0.0.1:8091` (localhost only, Caddy handles external access)
- 1 worker, 4 threads (appropriate for Raspberry Pi)
- 300s timeout for long-running API calls
- Auto-restarts on crash with 5s delay

### btcedu-detect.timer

- Runs `btcedu detect` every 6 hours (`OnCalendar=*-*-* 00/6:00:00`)
- Random delay up to 5 minutes to avoid thundering herd
- Persistent: catches up on missed runs after reboot

### btcedu-run.timer

- Runs `btcedu run-latest` daily at 02:00 (`OnCalendar=*-*-* 02:00:00`)
- Random delay up to 10 minutes
- 30-minute timeout (`TimeoutStartSec=1800`)
- Persistent: catches up after reboot

---

## Verification Checklist

```bash
# Web service running
sudo systemctl status btcedu-web.service | grep "active (running)"

# All services enabled
sudo systemctl is-enabled btcedu-web.service btcedu-detect.timer btcedu-run.timer

# Port listening
sudo ss -tlnp | grep 8091

# Health check
curl -f http://127.0.0.1:8091/api/health

# Timers active with next trigger times
sudo systemctl list-timers btcedu-*

# Sudo works without password
sudo -n systemctl restart btcedu-web.service
```

---

## Monitoring and Logs

```bash
# Live web service logs
sudo journalctl -u btcedu-web -f

# Detection logs (last run)
sudo journalctl -u btcedu-detect -n 30

# Pipeline processing logs (last run)
sudo journalctl -u btcedu-run -n 50

# Timer schedules and next trigger
sudo systemctl list-timers btcedu-*

# Logs since today
sudo journalctl -u btcedu-web --since today

# Application file logs
tail -f data/logs/web_errors.log
```

---

## Troubleshooting

### Service fails to start

```bash
sudo journalctl -u btcedu-web -n 100 --no-pager
```

| Error | Fix |
|-------|-----|
| `gunicorn: No such file or directory` | `.venv/bin/pip install -e ".[web]"` |
| `No such file or directory: '.env'` | Create `.env` from template |
| `no such table: episodes` | `.venv/bin/btcedu init-db && .venv/bin/btcedu migrate` |
| `Address already in use` | `sudo lsof -i :8091` and kill the process |

### Service starts but crashes

```bash
sudo journalctl -u btcedu-web -f
```

Common causes: missing API keys in `.env`, Python import errors (reinstall deps), file permission errors.

### Timers not firing

```bash
# Check next trigger time
sudo systemctl list-timers btcedu-*

# Manually trigger
sudo systemctl start btcedu-detect.service
sudo systemctl start btcedu-run.service

# Check if timer is enabled
sudo systemctl is-enabled btcedu-detect.timer btcedu-run.timer
```

### run.sh cannot restart services

```bash
# Test sudo
sudo -n systemctl restart btcedu-web

# If password prompted, reconfigure sudoers (see section 3 above)
```

### Service doesn't start on boot

```bash
sudo systemctl is-enabled btcedu-web.service
# If "disabled":
sudo systemctl enable btcedu-web.service
```

---

## Security

- **Network binding**: `127.0.0.1:8091` — not exposed to internet, Caddy handles HTTPS
- **Environment file**: `.env` with `chmod 600` — API keys readable only by pi user
- **Sudo permissions**: Scoped to specific systemctl commands only
- **Basic auth**: Dashboard behind Caddy basic auth with bcrypt password hash
- **Headers**: `X-Content-Type-Options`, `X-Frame-Options DENY`, `Referrer-Policy no-referrer`

---

## Maintenance

```bash
# Restart web service
sudo systemctl restart btcedu-web

# Stop everything
sudo systemctl stop btcedu-web btcedu-detect.timer btcedu-run.timer

# After editing .service files
sudo systemctl daemon-reload
sudo systemctl restart btcedu-web

# Full redeployment
./run.sh
```
