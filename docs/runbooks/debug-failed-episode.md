# Runbook: Debugging a Failed Episode

## Step 1: Identify the failure

```bash
# Check episode status and error
btcedu report --episode-id EPISODE_ID

# Or query directly
.venv/bin/python -c "
from btcedu.db import get_engine, get_session_factory
from btcedu.models.episode import Episode
engine = get_engine()
Session = get_session_factory(engine.url)
with Session() as s:
    ep = s.query(Episode).filter(Episode.episode_id == 'EPISODE_ID').first()
    print(f'Status: {ep.status.value}')
    print(f'Error: {ep.error_message}')
    print(f'Pipeline version: {ep.pipeline_version}')
    print(f'Retries: {ep.retry_count}')
"
```

## Step 2: Check pipeline run history

```bash
.venv/bin/python -c "
from btcedu.db import get_engine, get_session_factory
from btcedu.models.episode import Episode, PipelineRun
engine = get_engine()
Session = get_session_factory(engine.url)
with Session() as s:
    ep = s.query(Episode).filter(Episode.episode_id == 'EPISODE_ID').first()
    runs = s.query(PipelineRun).filter(PipelineRun.episode_id == ep.id).order_by(PipelineRun.started_at.desc()).limit(10).all()
    for r in runs:
        print(f'{r.stage} | {r.status} | cost=\${r.estimated_cost_usd or 0:.4f} | err={r.error_message or \"-\"}')
"
```

## Step 3: Check common causes

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| "v1 pipeline" error on v2 stage | `pipeline_version=1` | Set `pipeline_version=2` in DB |
| ffmpeg timeout | Segment too slow for Pi | Increase `RENDER_TIMEOUT_SEGMENT` in `.env` |
| Cost limit exceeded | Episode hit `max_episode_cost_usd` | Increase limit or skip expensive stages |
| Missing API key | Key not in `.env` | Add the key |
| Stale error still showing | `error_message` not cleared on success | Bug — clear it manually or update code |
| Review pending | Pipeline waiting for human approval | `btcedu review list --status pending` |

## Step 4: Check artifacts

```bash
ls -la data/outputs/EPISODE_ID/
ls -la data/outputs/EPISODE_ID/images/
ls -la data/outputs/EPISODE_ID/review/

# Check for stale markers
find data/outputs/EPISODE_ID/ -name "*.stale"
```

## Step 5: Retry

```bash
# Clear error and retry
btcedu retry --episode-id EPISODE_ID

# Or force re-run a specific stage
btcedu render --episode-id EPISODE_ID --force
```

## Step 6: Manual fix (if needed)

```bash
# Fix pipeline_version mismatch
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
    print('Fixed.')
"
```
