---
name: pipeline-status
description: Show current pipeline status — episode counts by status, pending reviews, and recent errors
allowed-tools: Bash
---

Show the current btcedu pipeline status.

```bash
.venv/bin/python -c "
from btcedu.db import get_engine, get_session_factory
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.review import ReviewTask
from collections import Counter
engine = get_engine()
Session = get_session_factory(engine.url)
with Session() as s:
    episodes = s.query(Episode).all()
    counts = Counter(ep.status.value for ep in episodes)
    print('=== Episode Status ===')
    for status, count in sorted(counts.items()):
        print(f'  {status}: {count}')
    print(f'  TOTAL: {len(episodes)}')

    # Errors
    errors = [ep for ep in episodes if ep.error_message]
    if errors:
        print()
        print('=== Episodes with Errors ===')
        for ep in errors:
            print(f'  {ep.episode_id} ({ep.status.value}): {ep.error_message[:80]}')

    # Pending reviews
    pending = s.query(ReviewTask).filter(ReviewTask.status == 'pending').all()
    if pending:
        print()
        print('=== Pending Reviews ===')
        for r in pending:
            print(f'  Review #{r.id} | {r.episode_id} | stage={r.stage}')
    else:
        print()
        print('No pending reviews.')
"
```

Report the status summary concisely.
