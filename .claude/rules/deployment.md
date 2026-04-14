---
paths:
  - deploy/**
  - run.sh
---

# Deployment Rules

- Deploy via `./run.sh`: git pull → pip install → migrate → restart services
- Target: Raspberry Pi with systemd timers + Caddy reverse proxy
- ffmpeg uses software encoding on Pi (slow) — use `RENDER_PRESET=ultrafast`, `RENDER_TIMEOUT_SEGMENT=900`
- YouTube deps are optional: `pip install -e ".[youtube]"`. `run.sh` auto-installs if `data/client_secret.json` exists
- Migrations are idempotent (check-before-act). Run `btcedu migrate` after updates
- Never expose `.env` secrets, password hashes, or local machine paths in committed files
