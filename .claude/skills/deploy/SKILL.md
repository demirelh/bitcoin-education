---
name: deploy
description: Deploy the btcedu application by running run.sh (git pull, pip install, migrate, restart services)
allowed-tools: Bash, Read
---

Run the btcedu deployment script and verify success.

Steps:
1. Run `bash run.sh` from the project root
2. Check the output for any failures
3. Verify the web service is healthy: `curl -sf http://localhost:8091/api/health`
4. If deployment fails, check logs: `sudo journalctl -u btcedu-web -n 30 --no-pager`

```bash
bash run.sh 2>&1
```

Report whether deployment succeeded or failed, and highlight any warnings.
