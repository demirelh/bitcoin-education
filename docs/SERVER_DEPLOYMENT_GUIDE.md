# Server Deployment Guide - btcedu-web Service

This guide provides complete instructions to install and configure the `btcedu-web.service` on the production server to enable fully automated GitHub Actions deployments.

## Current Status

✅ **Working:**
- GitHub Actions workflow configured correctly
- SSH authentication and connection successful
- `run.sh` deployment script executes properly
- Git pull, dependency installation, and migrations run successfully

❌ **Missing:**
- `btcedu-web.service` not installed on the server
- Service cannot be restarted by automated deployments

## Goal

Install and configure the systemd service so that:
- The web dashboard runs continuously as a background service
- Automated deployments can restart the service via `run.sh`
- The service starts automatically on server boot
- The service restarts automatically on failure

---

## Prerequisites

Before installing the service, ensure:

1. **Virtual environment exists:**
   ```bash
   cd /home/pi/AI-Startup-Lab/bitcoin-education
   test -d .venv && echo "✓ venv exists" || echo "✗ venv missing"
   ```

2. **Gunicorn is installed:**
   ```bash
   .venv/bin/pip install 'gunicorn>=22.0.0'
   ```

3. **Environment file exists:**
   ```bash
   test -f .env && echo "✓ .env exists" || echo "✗ .env missing"
   ```
   If missing, copy from example:
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

4. **Application is installed:**
   ```bash
   .venv/bin/pip install -e ".[web]"
   ```

5. **Database is initialized:**
   ```bash
   .venv/bin/btcedu init-db
   .venv/bin/btcedu migrate
   ```

---

## Service Installation

### Method 1: Automated (Recommended)

Use the provided setup script:

```bash
cd /home/pi/AI-Startup-Lab/bitcoin-education
./deploy/setup-web.sh
```

This script will:
1. Install gunicorn
2. Copy the service file to systemd
3. Enable and start the service
4. Guide you through Caddy configuration (optional, for HTTPS access)

### Method 2: Manual Installation

Follow these steps to manually install the service:

#### Step 1: Copy Service File

```bash
sudo cp /home/pi/AI-Startup-Lab/bitcoin-education/deploy/btcedu-web.service /etc/systemd/system/
```

#### Step 2: Reload Systemd

```bash
sudo systemctl daemon-reload
```

#### Step 3: Enable Service (Start on Boot)

```bash
sudo systemctl enable btcedu-web.service
```

#### Step 4: Start Service

```bash
sudo systemctl start btcedu-web.service
```

#### Step 5: Verify Service is Running

```bash
sudo systemctl status btcedu-web.service
```

Expected output should show:
- `Active: active (running)` in green
- Process ID and memory usage
- Recent log entries

---

## Service Configuration Details

The `btcedu-web.service` file contains:

```ini
[Unit]
Description=btcedu web dashboard (gunicorn)
After=network.target

[Service]
Type=notify
User=pi
WorkingDirectory=/home/pi/AI-Startup-Lab/bitcoin-education
ExecStart=/home/pi/AI-Startup-Lab/bitcoin-education/.venv/bin/gunicorn \
    -w 1 \
    --threads 4 \
    -b 127.0.0.1:8091 \
    --timeout 300 \
    "btcedu.web.app:create_app()"
EnvironmentFile=/home/pi/AI-Startup-Lab/bitcoin-education/.env
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### Key Configuration Elements:

- **Type=notify:** Gunicorn sends readiness notification to systemd
- **User=pi:** Runs as the pi user (same as your login user)
- **WorkingDirectory:** Project root where .env and data/ exist
- **ExecStart:** Gunicorn with 1 worker, 4 threads, listening on localhost:8091
- **EnvironmentFile:** Loads API keys and configuration from .env
- **Restart=on-failure:** Automatically restarts if the process crashes
- **RestartSec=5:** Waits 5 seconds before attempting restart
- **StandardOutput/Error=journal:** Logs to journalctl for easy debugging

---

## Sudo Configuration for Non-Interactive Restart

The `run.sh` script needs to restart the service via `sudo systemctl restart btcedu-web`. To allow this without password prompts during automated deployments:

### Option 1: Service-Specific Sudo (Recommended)

Grant sudo only for specific btcedu-web service commands:

```bash
# Create a sudoers file for pi user
echo "pi ALL=(ALL) NOPASSWD: /bin/systemctl restart btcedu-web, /bin/systemctl status btcedu-web, /bin/systemctl stop btcedu-web, /bin/systemctl start btcedu-web, /bin/systemctl is-active btcedu-web" | sudo tee /etc/sudoers.d/pi-btcedu

# Set correct permissions
sudo chmod 0440 /etc/sudoers.d/pi-btcedu

# Verify syntax
sudo visudo -c
```

### Option 2: Full Systemctl Sudo (Less Secure)

If you need broader systemctl access:

```bash
echo "pi ALL=(ALL) NOPASSWD: /bin/systemctl" | sudo tee /etc/sudoers.d/pi-systemctl
sudo chmod 0440 /etc/sudoers.d/pi-systemctl
sudo visudo -c
```

### Test Sudo Configuration

```bash
# Should execute without password prompt
sudo -n systemctl restart btcedu-web

# If it asks for password, sudo is not configured correctly
```

---

## Verification Checklist

After installation, verify everything is working:

### 1. Service Status

```bash
sudo systemctl status btcedu-web.service
```

**Expected:**
- ✅ `Loaded: loaded (...; enabled; ...)`
- ✅ `Active: active (running)`
- ✅ No errors in recent logs

### 2. Service Logs

```bash
sudo journalctl -u btcedu-web.service -n 50
```

**Expected:**
- ✅ Gunicorn started successfully
- ✅ Flask app initialized
- ✅ Listening on 127.0.0.1:8091
- ✅ No error messages

### 3. Process Running

```bash
ps aux | grep gunicorn
```

**Expected:**
- ✅ At least one gunicorn process owned by user `pi`
- ✅ Process includes `btcedu.web.app:create_app()`

### 4. Port Listening

```bash
sudo ss -tlnp | grep 8091
```

**Expected:**
- ✅ `127.0.0.1:8091` in LISTEN state
- ✅ Process name shows gunicorn

### 5. Health Check

```bash
curl -f http://127.0.0.1:8091/api/health
```

**Expected:**
- ✅ HTTP 200 response
- ✅ JSON response: `{"status":"ok"}`

### 6. Service Restart Test

```bash
sudo systemctl restart btcedu-web.service
sleep 3
sudo systemctl status btcedu-web.service
```

**Expected:**
- ✅ Service stops and starts cleanly
- ✅ Returns to `active (running)` state
- ✅ New process ID assigned

### 7. Boot Persistence Test

```bash
sudo systemctl is-enabled btcedu-web.service
```

**Expected:**
- ✅ Output: `enabled`

### 8. Automated Deployment Test

Trigger a GitHub Actions deployment:

**Option A: Manual Workflow Dispatch**
1. Go to: https://github.com/demirelh/bitcoin-education/actions/workflows/deploy.yml
2. Click "Run workflow" → Select branch "main" → Click "Run workflow"

**Option B: Push to Main**
```bash
# Make a trivial change
echo "# Test deployment" >> README.md
git add README.md
git commit -m "Test automated deployment"
git push origin main
```

**Verify:**
1. Watch workflow run at: https://github.com/demirelh/bitcoin-education/actions
2. Workflow should complete successfully
3. On server, check logs:
   ```bash
   sudo journalctl -u btcedu-web.service -n 20
   ```
4. Look for restart timestamp matching deployment time

---

## Troubleshooting

### Service Fails to Start

**Symptom:** `systemctl status` shows "failed" or "activating"

**Diagnosis:**
```bash
sudo journalctl -u btcedu-web.service -n 100 --no-pager
```

**Common causes:**

1. **Virtual environment missing:**
   ```
   Error: /home/pi/AI-Startup-Lab/bitcoin-education/.venv/bin/gunicorn: No such file or directory
   ```
   **Fix:** Create venv and install dependencies
   ```bash
   cd /home/pi/AI-Startup-Lab/bitcoin-education
   python -m venv .venv
   .venv/bin/pip install -e ".[web]"
   ```

2. **Gunicorn not installed:**
   ```
   Error: No module named 'gunicorn'
   ```
   **Fix:** Install gunicorn
   ```bash
   .venv/bin/pip install 'gunicorn>=22.0.0'
   ```

3. **Missing .env file:**
   ```
   Error: No such file or directory: '/home/pi/.../bitcoin-education/.env'
   ```
   **Fix:** Create .env from example
   ```bash
   cp .env.example .env
   # Edit with your API keys
   ```

4. **Database not initialized:**
   ```
   Error: no such table: episodes
   ```
   **Fix:** Initialize database
   ```bash
   .venv/bin/btcedu init-db
   .venv/bin/btcedu migrate
   ```

5. **Port already in use:**
   ```
   Error: Address already in use
   ```
   **Fix:** Kill existing process or change port
   ```bash
   sudo lsof -i :8091
   # Kill the process or edit service file to use different port
   ```

### Service Starts But Crashes

**Symptom:** Service starts then immediately fails

**Diagnosis:**
```bash
sudo journalctl -u btcedu-web.service -f
```

**Check:**
- Python import errors → reinstall dependencies
- Missing API keys in .env → add keys
- Database schema errors → run migrations
- File permission errors → check ownership

### Run.sh Cannot Restart Service

**Symptom:** `run.sh` reports "Failed to restart btcedu-web service"

**Diagnosis:**
```bash
# Test sudo manually
sudo -n systemctl restart btcedu-web
```

**If it prompts for password:**
- Sudo is not configured for non-interactive use
- Follow "Sudo Configuration" section above

### Service Doesn't Start on Boot

**Symptom:** After server reboot, service is not running

**Check:**
```bash
sudo systemctl is-enabled btcedu-web.service
```

**If output is "disabled":**
```bash
sudo systemctl enable btcedu-web.service
```

---

## Monitoring and Logs

### View Live Logs

```bash
# Follow logs in real-time
sudo journalctl -u btcedu-web.service -f

# Last 50 lines
sudo journalctl -u btcedu-web.service -n 50

# Logs since 1 hour ago
sudo journalctl -u btcedu-web.service --since "1 hour ago"

# Logs from today
sudo journalctl -u btcedu-web.service --since today
```

### Check Service Resource Usage

```bash
systemctl status btcedu-web.service
```

Shows:
- Memory usage
- CPU usage
- Process tree
- Recent log lines

### Application-Specific Logs

The web app also writes logs to files:

```bash
# General request logs
tail -f /home/pi/AI-Startup-Lab/bitcoin-education/data/logs/web.log

# Error logs
tail -f /home/pi/AI-Startup-Lab/bitcoin-education/data/logs/web_errors.log
```

---

## Maintenance Commands

### Restart Service

```bash
sudo systemctl restart btcedu-web.service
```

### Stop Service

```bash
sudo systemctl stop btcedu-web.service
```

### Start Service

```bash
sudo systemctl start btcedu-web.service
```

### Reload Service (After Editing .service File)

```bash
sudo systemctl daemon-reload
sudo systemctl restart btcedu-web.service
```

### Disable Service (Prevent Auto-Start on Boot)

```bash
sudo systemctl disable btcedu-web.service
```

### Check Service Status

```bash
sudo systemctl status btcedu-web.service
```

---

## Security Considerations

### Service Binding

The service binds to `127.0.0.1:8091` (localhost only). This means:
- ✅ Service is NOT exposed directly to the internet
- ✅ Only accessible from the same machine
- ✅ Must use reverse proxy (Caddy) for external access

### Environment Variables

API keys are stored in `/home/pi/AI-Startup-Lab/bitcoin-education/.env`:
- ✅ File should have permissions `600` (read/write by pi user only)
- ✅ Never commit .env to git
- ✅ Service loads environment via `EnvironmentFile=` directive

### Sudo Permissions

The pi user has passwordless sudo for systemctl commands:
- ✅ Limited to specific btcedu-web service commands only
- ✅ Cannot execute arbitrary commands with sudo
- ✅ Allows automated deployments without storing passwords

---

## Next Steps

Once the service is installed and verified:

1. **Test automated deployment:**
   - Push a change to main branch
   - Verify GitHub Actions workflow completes successfully
   - Confirm service restarts automatically
   - Check logs to ensure no errors

2. **Configure public access (optional):**
   - Follow instructions in `deploy/setup-web.sh` to configure Caddy
   - Set up basic auth for dashboard access
   - Access dashboard at: https://lnodebtc.duckdns.org/dashboard/

3. **Monitor for issues:**
   - Check service status daily: `systemctl status btcedu-web`
   - Review logs for errors: `journalctl -u btcedu-web --since today`
   - Verify automated deployments are working

4. **Update verification report:**
   - Update `DEPLOYMENT_VERIFICATION_REPORT.md` with PASS status for all requirements
   - Document successful end-to-end deployment test

---

## Summary

**Before Installation:**
- GitHub Actions workflow: ✅ Working
- SSH connection: ✅ Working
- run.sh execution: ✅ Working
- Service restart: ❌ Service not installed

**After Installation:**
- GitHub Actions workflow: ✅ Working
- SSH connection: ✅ Working
- run.sh execution: ✅ Working
- Service restart: ✅ Working
- **Result:** Fully automated end-to-end deployment! 🎉

---

## Quick Reference

```bash
# Install service
sudo cp deploy/btcedu-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btcedu-web

# Configure sudo
echo "pi ALL=(ALL) NOPASSWD: /bin/systemctl restart btcedu-web, /bin/systemctl status btcedu-web, /bin/systemctl stop btcedu-web, /bin/systemctl start btcedu-web, /bin/systemctl is-active btcedu-web" | sudo tee /etc/sudoers.d/pi-btcedu
sudo chmod 0440 /etc/sudoers.d/pi-btcedu

# Verify
sudo systemctl status btcedu-web
curl http://127.0.0.1:8091/api/health
sudo journalctl -u btcedu-web -n 20

# Test deployment
# Push to main branch or trigger workflow manually
```
