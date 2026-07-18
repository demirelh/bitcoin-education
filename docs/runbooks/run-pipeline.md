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
btcedu transcript-analyze --episode-id EPISODE_ID
btcedu transcript-verify --episode-id EPISODE_ID --dry-run
btcedu transcript-verify --episode-id EPISODE_ID
btcedu correct --episode-id EPISODE_ID
btcedu transcript-qa --episode-id EPISODE_ID
btcedu translate --episode-id EPISODE_ID
btcedu adapt --episode-id EPISODE_ID
btcedu translation-qa --episode-id EPISODE_ID
btcedu chapterize --episode-id EPISODE_ID
btcedu imagegen --episode-id EPISODE_ID
btcedu tts --episode-id EPISODE_ID
btcedu render --episode-id EPISODE_ID
btcedu publish --episode-id EPISODE_ID
```

The deterministic analyze/QA commands accept `--force` and always cost zero.
`transcript-verify` accepts `--dry-run` because it normally extracts audio and
calls the secondary ASR provider. Other stage flags are shown by
`btcedu COMMAND --help`.

QA command exit codes are scriptable:

- `0`: completed and non-blocking
- `1`: missing input or execution failure
- `2`: completed successfully but produced a blocking RED result

## Checking status

```bash
btcedu status                    # summary by episode status
btcedu report --episode-id ID    # detailed single-episode report
btcedu cost                      # cost breakdown
btcedu review list               # pending review tasks
btcedu migrate-status            # applied and pending migrations
```

## Review gates

The pipeline pauses at review gates. See `docs/runbooks/handle-review-gates.md`.
For translation QA, automatic repairs are limited by
`quality_gate.max_automatic_retries` in the profile. `retry` resumes from the
episode's current status; it does not reset QA retry history.

## Deployment

```bash
./run.sh    # git pull, pip install, migrate, restart services
```

After deployment, run:

```bash
btcedu migrate-status
btcedu status
curl -f http://127.0.0.1:8091/api/health
```
