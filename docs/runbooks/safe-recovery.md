# Runbook: Safe Recovery Paths

## Episode stuck in wrong status

```bash
# Check current state
btcedu report --episode-id EPISODE_ID

# Fix via Python
.venv/bin/python -c "
from btcedu.db import get_engine, get_session_factory
from btcedu.models.episode import Episode, EpisodeStatus
engine = get_engine()
Session = get_session_factory(engine.url)
with Session() as s:
    ep = s.query(Episode).filter(Episode.episode_id == 'EPISODE_ID').first()
    ep.status = EpisodeStatus.CHAPTERIZED  # set to desired status
    ep.error_message = None
    s.commit()
"
```

## Pipeline version mismatch

If a v1 episode is stuck in a v2 status (e.g., IMAGES_GENERATED with pipeline_version=1):

```bash
.venv/bin/python -c "
from btcedu.db import get_engine, get_session_factory
from btcedu.models.episode import Episode
engine = get_engine()
Session = get_session_factory(engine.url)
with Session() as s:
    ep = s.query(Episode).filter(Episode.episode_id == 'EPISODE_ID').first()
    ep.pipeline_version = 2
    ep.error_message = None
    s.commit()
"
```

## Stale error message persisting

If an episode succeeded but still shows an old error in the dashboard:

```bash
.venv/bin/python -c "
from btcedu.db import get_engine, get_session_factory
from btcedu.models.episode import Episode
engine = get_engine()
Session = get_session_factory(engine.url)
with Session() as s:
    ep = s.query(Episode).filter(Episode.episode_id == 'EPISODE_ID').first()
    ep.error_message = None
    s.commit()
"
```

## Re-run a single stage

```bash
# Force re-run (bypasses idempotency checks)
btcedu correct --episode-id EPISODE_ID --force
btcedu translate --episode-id EPISODE_ID --force
btcedu render --episode-id EPISODE_ID --force
```

## Clear stale markers

```bash
# Find stale markers for an episode
find data/outputs/EPISODE_ID/ -name "*.stale"

# Remove them to prevent unnecessary re-processing
find data/outputs/EPISODE_ID/ -name "*.stale" -delete
```

## Reset a failed episode for retry

```bash
btcedu retry --episode-id EPISODE_ID
```

This clears `error_message` and re-runs the pipeline from the current status.

## Database backup/restore

```bash
# Backup
cp data/btcedu.db data/btcedu.db.backup

# Restore
cp data/btcedu.db.backup data/btcedu.db
sudo systemctl restart btcedu-web
```

## Service recovery

```bash
# Check service status
sudo systemctl status btcedu-web

# Restart
sudo systemctl restart btcedu-web

# Check logs
sudo journalctl -u btcedu-web -n 50 --no-pager

# Full redeploy
./run.sh
```
