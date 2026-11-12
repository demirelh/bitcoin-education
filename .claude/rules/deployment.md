---
paths:
  - deploy/**
  - run.sh
---

# Deployment Rules

- Deploy via `./run.sh`: git pull → pip install → migrate → restart services
- `run.sh` refuses to run between 19:30 and 23:30 (evening broadcast); override only with `ALLOW_BROADCAST_WINDOW=1`
- `run.sh` refuses to run from a linked git worktree — a worktree has no `.venv`/`.env` and would trigger the initial setup instead of an update
- Target: Raspberry Pi with systemd timers + Caddy reverse proxy
- ffmpeg uses software encoding on Pi (slow) — production tuning `RENDER_PRESET=veryfast`, `RENDER_CRF=16`, `RENDER_TIMEOUT_SEGMENT=3600` (see the Raspberry Pi gotcha in `CLAUDE.md`)
- YouTube deps are optional: `pip install -e ".[youtube]"`. `run.sh` auto-installs if `data/client_secret.json` exists
- Migrations are idempotent (check-before-act). Run `btcedu migrate` after updates
- Never expose `.env` secrets, password hashes, or local machine paths in committed files
