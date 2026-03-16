# Runbook: Running the Pipeline

## Automated operation

The pipeline runs automatically via systemd timers:
- `btcedu-detect.timer` — scans RSS feed for new episodes
- `btcedu-run.timer` — processes actionable episodes (`run_pending`)

Check timer status:
```bash
systemctl list-timers btcedu-*
```

## Manual single-episode run

```bash
# Run a specific episode through all applicable stages
btcedu run --episode-id EPISODE_ID

# Run with force (re-run even if up-to-date)
btcedu run --episode-id EPISODE_ID --force

# Dry-run (no real API calls, placeholders instead)
btcedu run --episode-id EPISODE_ID --dry-run
```

## Manual batch run

```bash
# Process all actionable episodes
btcedu run-pending --max 5

# Run latest detected episode
btcedu run-latest
```

## Running individual stages

```bash
btcedu correct --episode-id EPISODE_ID
btcedu translate --episode-id EPISODE_ID
btcedu adapt --episode-id EPISODE_ID
btcedu chapterize --episode-id EPISODE_ID
btcedu imagegen --episode-id EPISODE_ID
btcedu tts --episode-id EPISODE_ID
btcedu render --episode-id EPISODE_ID
btcedu publish --episode-id EPISODE_ID
```

All stages accept `--force` and `--dry-run` flags.

## Checking status

```bash
btcedu status                    # summary by episode status
btcedu report --episode-id ID    # detailed single-episode report
btcedu cost                      # cost breakdown
btcedu review list               # pending review tasks
```

## Review gates

The pipeline pauses at review gates. See `docs/runbooks/handle-review-gates.md`.

## Deployment

```bash
./run.sh    # git pull, pip install, migrate, restart services
```
