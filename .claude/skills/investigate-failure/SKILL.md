---
name: investigate-failure
description: Investigate why a specific episode failed in the pipeline
argument-hint: "<episode_id>"
allowed-tools: Bash, Read, Grep
---

Investigate the failure of episode `$ARGUMENTS`.

Run the following diagnostic queries:

```bash
.venv/bin/python -c "
from btcedu.db import get_engine, get_session_factory
from btcedu.models.episode import Episode, PipelineRun
engine = get_engine()
Session = get_session_factory(engine.url)
with Session() as s:
    ep = s.query(Episode).filter(Episode.episode_id == '$ARGUMENTS').first()
    if not ep:
        print('Episode not found: $ARGUMENTS')
        exit(1)
    print(f'Episode: {ep.episode_id}')
    print(f'Title: {ep.title}')
    print(f'Status: {ep.status.value}')
    print(f'Pipeline version: {ep.pipeline_version}')
    print(f'Error: {ep.error_message}')
    print(f'Retry count: {ep.retry_count}')
    print()
    runs = s.query(PipelineRun).filter(PipelineRun.episode_id == ep.id).order_by(PipelineRun.started_at.desc()).limit(10).all()
    print('Recent pipeline runs:')
    for r in runs:
        print(f'  {r.stage} | {r.status} | {r.started_at} | err={r.error_message or \"-\"}')
"
```

Then check:
1. Does `data/outputs/$ARGUMENTS/` exist? What artifacts are present?
2. Are there `.stale` markers?
3. Is there a pending ReviewTask blocking progress?
4. Is `pipeline_version` correct for the episode's current status?

Report findings with root cause and recommended fix.
