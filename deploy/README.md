# Deployment Files

This directory contains all files needed for production deployment of the btcedu application.

## Quick Start - Install Service on Server

**Run this on the server (lnodebtc.duckdns.org) as user pi:**

```bash
cd /home/pi/AI-Startup-Lab/bitcoin-education

# Option 1: Automated setup (recommended)
./deploy/setup-web.sh

# Option 2: Manual installation
sudo cp deploy/btcedu-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btcedu-web.service

# Configure passwordless sudo for automated deployments
echo "pi ALL=(ALL) NOPASSWD: /bin/systemctl restart btcedu-web, /bin/systemctl status btcedu-web, /bin/systemctl stop btcedu-web, /bin/systemctl start btcedu-web, /bin/systemctl is-active btcedu-web" | sudo tee /etc/sudoers.d/pi-btcedu
sudo chmod 0440 /etc/sudoers.d/pi-btcedu

# Verify installation
sudo systemctl status btcedu-web.service
curl http://127.0.0.1:8091/api/health
```

📖 **For detailed instructions, see:** [docs/SERVER_DEPLOYMENT_GUIDE.md](../docs/SERVER_DEPLOYMENT_GUIDE.md)

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
| `setup-web.sh` | Interactive setup script for web dashboard deployment |

---

## Deployment Architecture

```
GitHub Actions Workflow (deploy.yml)
    ↓ (push to main)
SSH to lnodebtc.duckdns.org
    ↓
run.sh deployment script
    ├─ git pull origin main
    ├─ pip install dependencies
    ├─ btcedu migrate (database)
    └─ sudo systemctl restart btcedu-web ← Requires service installation
           ↓
    btcedu-web.service (systemd)
        └─ gunicorn → Flask app (port 8091)
```

---

## Service Details

### btcedu-web.service

**What it does:**
- Runs the web dashboard continuously in the background
- Listens on `127.0.0.1:8091` (localhost only, not exposed to internet)
- Automatically restarts on failure
- Starts on server boot
- Logs to journalctl

**Service configuration:**
- **User:** pi
- **Working Directory:** /home/pi/AI-Startup-Lab/bitcoin-education
- **Command:** gunicorn with 1 worker, 4 threads
- **Environment:** Loads from .env file
- **Restart Policy:** on-failure with 5 second delay

**View logs:**
```bash
sudo journalctl -u btcedu-web.service -f
```

**Restart service:**
```bash
sudo systemctl restart btcedu-web.service
```

---

## Automated Deployment Flow

Once the service is installed, deployments work automatically:

1. **Developer pushes to main branch**
   ```bash
   git push origin main
   ```

2. **GitHub Actions triggers** (`.github/workflows/deploy.yml`)
   - Workflow runs automatically on push to main

3. **GitHub Actions connects via SSH**
   - Uses DEPLOY_SSH_KEY secret
   - Connects to pi@lnodebtc.duckdns.org

4. **Runs deployment script**
   ```bash
   bash /home/pi/AI-Startup-Lab/bitcoin-education/run.sh
   ```

5. **run.sh executes deployment steps:**
   - ✅ Pulls latest code from git
   - ✅ Installs/updates Python dependencies
   - ✅ Runs database migrations
   - ✅ Restarts btcedu-web service ← **Requires service to be installed**

6. **Service restarts**
   - Gunicorn reloads with new code
   - Dashboard updates are live
   - Zero downtime (systemd manages graceful restart)

---

## Prerequisites for Service Installation

Before installing btcedu-web.service, ensure:

1. ✅ Virtual environment exists: `/home/pi/AI-Startup-Lab/bitcoin-education/.venv`
2. ✅ Gunicorn is installed: `.venv/bin/pip install 'gunicorn>=22.0.0'`
3. ✅ Environment file exists: `.env` (copy from `.env.example`)
4. ✅ Application is installed: `.venv/bin/pip install -e ".[web]"`
5. ✅ Database is initialized: `.venv/bin/btcedu init-db && .venv/bin/btcedu migrate`

---

## Verification Checklist

After installing the service, verify it works:

```bash
# 1. Service is running
sudo systemctl status btcedu-web.service | grep "active (running)"

# 2. Service is enabled (starts on boot)
sudo systemctl is-enabled btcedu-web.service | grep "enabled"

# 3. Port is listening
sudo ss -tlnp | grep 8091 | grep gunicorn

# 4. Health check passes
curl -f http://127.0.0.1:8091/api/health

# 5. Sudo restart works without password
sudo -n systemctl restart btcedu-web.service

# 6. Test automated deployment
# Go to: https://github.com/demirelh/bitcoin-education/actions/workflows/deploy.yml
# Click "Run workflow" and verify it completes successfully
```

---

## Troubleshooting

### Service fails to start

```bash
# Check logs for errors
sudo journalctl -u btcedu-web.service -n 50

# Common issues:
# - Virtual environment missing → create it
# - Gunicorn not installed → pip install gunicorn
# - .env file missing → copy from .env.example
# - Database not initialized → run btcedu init-db
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
echo "pi ALL=(ALL) NOPASSWD: /bin/systemctl restart btcedu-web, /bin/systemctl status btcedu-web, /bin/systemctl stop btcedu-web, /bin/systemctl start btcedu-web, /bin/systemctl is-active btcedu-web" | sudo tee /etc/sudoers.d/pi-btcedu
sudo chmod 0440 /etc/sudoers.d/pi-btcedu
```

---

## Additional Services (Optional)

### btcedu-detect (RSS Detection)

Automatically checks for new podcast episodes every 6 hours.

```bash
sudo cp deploy/btcedu-detect.service /etc/systemd/system/
sudo cp deploy/btcedu-detect.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btcedu-detect.timer
```

### btcedu-run (Episode Processing)

Automatically processes pending episodes daily at 02:00.

```bash
sudo cp deploy/btcedu-run.service /etc/systemd/system/
sudo cp deploy/btcedu-run.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btcedu-run.timer
```

---

## Public Access via Caddy (Optional)

To access the dashboard from the internet:

1. **Follow the setup-web.sh script** - it generates a password hash for basic auth
2. **Edit /etc/caddy/Caddyfile** - add the reverse proxy configuration
3. **Reload Caddy**: `sudo systemctl reload caddy`
4. **Access dashboard at**: https://lnodebtc.duckdns.org/dashboard/

See: [deploy/Caddyfile.dashboard](Caddyfile.dashboard) for the configuration snippet.

---

## Support

For detailed deployment instructions, troubleshooting, and architecture details:

📖 **[docs/SERVER_DEPLOYMENT_GUIDE.md](../docs/SERVER_DEPLOYMENT_GUIDE.md)**

For deployment verification and security audit:

📋 **[DEPLOYMENT_VERIFICATION_REPORT.md](../DEPLOYMENT_VERIFICATION_REPORT.md)**
