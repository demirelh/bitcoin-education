# Deployment Files

This directory contains all files needed for production deployment of the btcedu application.

## Quick Start - Install Services on Server

**Run this on the server (lnodebtc.duckdns.org) as user pi:**

```bash
cd /home/pi/AI-Startup-Lab/bitcoin-education

# Option 1: Automated setup (recommended)
./deploy/setup-web.sh

# Option 2: Manual installation
sudo cp deploy/btcedu-web.service /etc/systemd/system/
sudo cp deploy/btcedu-detect.service deploy/btcedu-detect.timer /etc/systemd/system/
sudo cp deploy/btcedu-run.service deploy/btcedu-run.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btcedu-web.service
sudo systemctl enable --now btcedu-detect.timer
sudo systemctl enable --now btcedu-run.timer

# Configure passwordless sudo for automated deployments
echo "pi ALL=(ALL) NOPASSWD: /bin/systemctl restart btcedu-web, /bin/systemctl status btcedu-web, /bin/systemctl stop btcedu-web, /bin/systemctl start btcedu-web, /bin/systemctl is-active btcedu-web, /bin/systemctl daemon-reload, /bin/systemctl restart btcedu-detect.timer, /bin/systemctl restart btcedu-run.timer" | sudo tee /etc/sudoers.d/pi-btcedu
sudo chmod 0440 /etc/sudoers.d/pi-btcedu

# Verify installation
sudo systemctl status btcedu-web.service
sudo systemctl list-timers btcedu-*
curl http://127.0.0.1:8091/api/health
```

For detailed instructions, see: [docs/SERVER_DEPLOYMENT_GUIDE.md](../docs/SERVER_DEPLOYMENT_GUIDE.md)

---

## Files in This Directory

### Systemd Service Units

| File | Purpose |
|------|---------|
| `btcedu-web.service` | Web dashboard service (gunicorn on port 8091) |
| `btcedu-detect.service` + `.timer` | Periodic RSS feed detection (every 6 hours) |
| `btcedu-run.service` + `.timer` | Periodic episode processing (daily at 02:00) |

### Configuration Files

| File | Purpose |
|------|---------|
| `Caddyfile.dashboard` | Caddy reverse proxy config snippet for HTTPS access |

### Setup Scripts

| File | Purpose |
|------|---------|
| `setup-web.sh` | Interactive setup: installs all systemd services/timers, configures Caddy auth |

---

## Deployment Architecture

```
GitHub Actions Workflow (deploy.yml)
    | (push to main)
SSH to lnodebtc.duckdns.org
    |
run.sh deployment script
    |- git pull origin main
    |- pip install dependencies
    |- btcedu migrate (database)
    |- sudo systemctl daemon-reload
    |- sudo systemctl restart btcedu-web
    |- sudo systemctl restart btcedu-detect.timer
    +- sudo systemctl restart btcedu-run.timer
           |
    btcedu-web.service (systemd)
        +- gunicorn -> Flask app (port 8091)
    btcedu-detect.timer (every 6h)
        +- btcedu detect
    btcedu-run.timer (daily 02:00)
        +- btcedu run-latest
```

---

## Service Details

### btcedu-web.service

- Runs the web dashboard continuously in the background
- Listens on `127.0.0.1:8091` (localhost only, not exposed to internet)
- Automatically restarts on failure (5s delay)
- Starts on server boot
- Logs to journalctl

**Configuration:** User `pi`, 1 gunicorn worker, 4 threads, environment from `.env`.

### btcedu-detect.timer

- Runs `btcedu detect` every 6 hours (with up to 5 min random delay)
- Scans RSS feed for new podcast episodes
- Persistent: catches up on missed runs after reboot

### btcedu-run.timer

- Runs `btcedu run-latest` daily at 02:00 (with up to 10 min random delay)
- Processes the latest pending episode through the v2 pipeline
- Timeout: 30 minutes (`TimeoutStartSec=1800`)

**View logs:**
```bash
sudo journalctl -u btcedu-web -f
sudo journalctl -u btcedu-detect -n 20
sudo journalctl -u btcedu-run -n 20
sudo systemctl list-timers btcedu-*
```

---

## Automated Deployment Flow

Once services are installed, deployments work automatically:

1. **Developer pushes to main branch**
2. **GitHub Actions triggers** (`.github/workflows/deploy.yml`)
3. **GitHub Actions connects via SSH** (uses `DEPLOY_SSH_KEY` secret)
4. **Runs `run.sh`**, which:
   - Pulls latest code from git
   - Installs/updates Python dependencies (`.[web]` + `.[youtube]` if configured)
   - Runs database migrations
   - Reloads systemd daemon (picks up unit file changes)
   - Restarts web service + pipeline timers

---

## Prerequisites

Before running `setup-web.sh` or manual installation, ensure:

1. Virtual environment exists: `.venv/` (create with `python -m venv .venv`)
2. Application is installed: `.venv/bin/pip install -e ".[web]"` (includes gunicorn)
3. Environment file exists: `.env` (systemd units load it via `EnvironmentFile`)
4. Database is initialized: `.venv/bin/btcedu init-db && .venv/bin/btcedu migrate`
5. (Optional) YouTube credentials: `pip install -e ".[youtube]"` + `data/client_secret.json`

---

## Verification Checklist

After installing services, verify everything works:

```bash
# 1. Web service is running
sudo systemctl status btcedu-web.service | grep "active (running)"

# 2. All services enabled (start on boot)
sudo systemctl is-enabled btcedu-web.service
sudo systemctl is-enabled btcedu-detect.timer
sudo systemctl is-enabled btcedu-run.timer

# 3. Port is listening
sudo ss -tlnp | grep 8091 | grep gunicorn

# 4. Health check passes
curl -f http://127.0.0.1:8091/api/health

# 5. Timers are active
sudo systemctl list-timers btcedu-*

# 6. Sudo restart works without password
sudo -n systemctl restart btcedu-web.service

# 7. Test automated deployment
# Go to: https://github.com/demirelh/bitcoin-education/actions/workflows/deploy.yml
# Click "Run workflow" and verify it completes successfully
```

---

## Public Access via Caddy

To access the dashboard from the internet:

1. **Run `setup-web.sh`** — it generates a password hash for basic auth
2. **Edit `/etc/caddy/Caddyfile`** — add the reverse proxy configuration (see [Caddyfile.dashboard](Caddyfile.dashboard))
3. **Reload Caddy**: `sudo systemctl reload caddy`
4. **Access dashboard at**: https://lnodebtc.duckdns.org/dashboard/

---

## Troubleshooting

### Service fails to start

```bash
# Check logs for errors
sudo journalctl -u btcedu-web.service -n 50

# Common issues:
# - Virtual environment missing -> python -m venv .venv
# - Dependencies not installed -> pip install -e ".[web]"
# - .env file missing -> create from template
# - Database not initialized -> btcedu init-db && btcedu migrate
```

### Service starts but crashes

```bash
# Watch logs in real-time
sudo journalctl -u btcedu-web.service -f

# Check application logs
tail -f /home/pi/AI-Startup-Lab/bitcoin-education/data/logs/web_errors.log
```

### run.sh cannot restart service

```bash
# Test sudo configuration
sudo -n systemctl restart btcedu-web.service

# If it asks for password, configure sudo:
echo "pi ALL=(ALL) NOPASSWD: /bin/systemctl restart btcedu-web, /bin/systemctl status btcedu-web, /bin/systemctl stop btcedu-web, /bin/systemctl start btcedu-web, /bin/systemctl is-active btcedu-web, /bin/systemctl daemon-reload, /bin/systemctl restart btcedu-detect.timer, /bin/systemctl restart btcedu-run.timer" | sudo tee /etc/sudoers.d/pi-btcedu
sudo chmod 0440 /etc/sudoers.d/pi-btcedu
```

### Timers not firing

```bash
# Check timer status and next trigger time
sudo systemctl list-timers btcedu-*

# Manually trigger a timer's service
sudo systemctl start btcedu-detect.service
sudo systemctl start btcedu-run.service
```

---

## Support

For detailed deployment instructions and architecture details:

[docs/SERVER_DEPLOYMENT_GUIDE.md](../docs/SERVER_DEPLOYMENT_GUIDE.md)
