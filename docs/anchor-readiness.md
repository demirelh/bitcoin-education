# Anchor readiness and avatar reconciliation

Two operator commands guard the ALMANYA24 presenter: one answers *may and can
this episode be presented at all*, the other closes out avatar jobs whose
outcome only a human can establish.

Both are free. Neither generates a video.

---

## `btcedu anchor-readiness`

```
btcedu anchor-readiness --profile tagesschau_tr
btcedu anchor-readiness --profile tagesschau_tr --json
btcedu anchor-readiness --profile tagesschau_tr --studio-mode opaque_mp4
btcedu anchor-readiness --profile tagesschau_tr --online
```

The default run is entirely offline: it reads the profile, the studio manifest,
the release record, the ledger and the local FFmpeg build. Nothing is uploaded
and no provider is contacted.

### Areas checked

| Area | What it establishes |
|---|---|
| `configuration` | provider is `heygen`, engine is exactly `avatar_iii`, the cost rate is positive, the anchor stage budget is 7 USD and not larger than the episode budget, the look pool has unique, active, non-placeholder ids, and no invented global avatar id is being relied on |
| `studio` | the production manifest exists and validates, every referenced asset is present, readable and inside the asset directory, the frame size and fps match the renderer, the display zone and safe areas are usable, and the opaque mode has a presenter-free zone or an occlusion mask |
| `ffmpeg` | `ffmpeg` and `ffprobe` are present and the filters the selected mode needs are available; in alpha mode, that alpha can be *decoded* (production never encodes alpha — HeyGen supplies the WebM and the output is H.264) |
| `pipeline` | `sceneplan` runs before `anchorgen`, `anchorgen` before `render`, the presenter assignment is configurable, the avatar job table and its migration exist, publishing is private with `auto_publish=false`, and the remote renderer can carry the studio assets |
| `ledger` | no avatar job is sitting in `reserved`, `reconcile_required` or `abandoned` |
| `rights` | the release record is present, consented, unrevoked, in date, covers the configured channel and territory, carries an operator approval and an AI disclosure text |
| `provider` | only with `--online` (see below) |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | ready |
| 1 | warnings only; technically startable |
| 2 | blocked |
| 3 | the check itself could not run |

A misspelled `--profile` is *not* a usage error: it is reported as a blocking
`config.profile_exists` finding, so scripts get the same report shape either way.

### `--online`

Optional, read-only, free. It authenticates against HeyGen and looks each
configured look up: does it exist, does it belong to the expected avatar, does
the provider list `avatar_iii` among its engines. There is no upload and no
generation — the client has no method that could perform one, and a test asserts
that.

The promise is printed before the first request rather than only documented.

Two deliberate distinctions:

* A **network failure or timeout is a warning**, not a block. An unreachable
  provider says nothing about the configuration, and an offline verdict must not
  be poisoned by a bad minute of connectivity. A **401/403 blocks** — that is an
  answer.
* An **absent** `supported_api_engines` field stays *unknown*. Reading silence
  as "does not support Avatar III" would block a working look on a field the
  provider never promised to send.

### JSON output

`--json` emits a versioned document. Abridged:

```json
{
  "schema_version": 1,
  "profile": "tagesschau_tr",
  "studio_mode": "alpha_webm",
  "online": false,
  "exit_code": 2,
  "summary": { "pass": 30, "warning": 1, "blocked": 4 },
  "checks": [
    {
      "check_id": "config.engine",
      "area": "configuration",
      "severity": "blocking",
      "status": "PASS",
      "detail": "Engine is 'avatar_iii'",
      "remedy": ""
    },
    {
      "check_id": "studio.manifest_present",
      "area": "studio",
      "severity": "blocking",
      "status": "BLOCKED",
      "detail": "No studio manifest at assets/almanya24/studio/manifest.json",
      "remedy": "Produce the phase 1 studio package, or start from manifest.example.json"
    }
  ]
}
```

Every blocking result carries a `remedy`; a test enforces that, because a gate
that stops the pipeline without saying what to do is just an outage.

No secret, full API key or absolute deployment path appears in either output
format.

### What it says today

Phase 1 (the real studio package, the real looks, the signed release) has not
happened, so the current run **is expected to fail** with exit code 2 and four
blocking findings. That is the honest answer, not a defect.

---

## `btcedu avatar-reconcile`

A HeyGen generation is billed when it starts. If a run dies between reserving a
clip and hearing the provider's answer, whether money was spent is exactly what
nobody knows — so the ledger holds the row and never retries it on its own.
These commands are how a human closes it.

```
btcedu avatar-reconcile list [--episode-id EP] [--all] [--json]
btcedu avatar-reconcile inspect --job-id 42 [--json]
btcedu avatar-reconcile attach  --job-id 42 --provider-job-id PROV --operator-ref ops-1 \
                                --note "matched by episode and duration" --confirm
btcedu avatar-reconcile resolve --job-id 42 --decision DECISION \
                                --note "..." --operator-ref ops-1 [--output-path ...]
```

`list` shows episode, scene, status, provider job id, reserved cost and age.
`inspect` adds the hashes, whether the clip is on disk, the manifest's view of
the scene, the audit trail so far, and the next action that cannot cost money.
Neither touches the provider.

### Decisions

| `--decision` | Meaning | Resulting status |
|---|---|---|
| `running` | provider confirms the job exists and is generating | `submitted` (polled again) |
| `delivered` | provider confirms it finished and the clip was collected | `completed` |
| `not-billed` | provider confirms it refused the request and charged nothing | `failed` (retryable) |
| `unresolved` | provider cannot identify the job | unchanged; stays blocked |
| `abandon` | operator gives the clip up | `abandoned`; cost stays on the episode |

Every decision requires `--note` and `--operator-ref`, and writes an audit row
with the before/after status and cost.

### The rules that cost money

* **A 404 is not proof that nothing was billed.** HeyGen keeps finished videos
  for a limited window, so a missing generation weeks later proves only that the
  retention period expired. `not-billed` is refused unless a provider job id is
  bound to the row; the safe answers are `unresolved` or `abandon`.
* **`abandon` does not zero the cost.** Giving a clip up is not a claim that it
  was free, and the budget keeps assuming it was billed.
* **Nothing is ever re-ordered automatically.** A `reserved`, `reconcile_required`
  or `abandoned` row makes the anchor stage stop rather than buy again.
* **A settled job is never reopened**, and an abandonment is final — new work
  needs a new content hash.
* **`attach` is its own step and demands `--confirm`.** Binding a hand-found job
  id is an assertion every later decision inherits.
* **Concurrent resolution is transactional.** The transition is a single
  conditional UPDATE; the loser of a race is told, not overwritten.

---

## Failure policy

While the avatar path is enabled, a missing, invalid or unreconciled presenter
clip **stops the episode**. There is no automatic fallback to the old
full-frame voice-over presentation: an episode that silently drops the presenter
renders, passes its technical checks and publishes in a format the channel
abandoned, without anyone being told.

A voice-over fallback remains possible, but only as an explicit operator
decision for **one named episode**, with a reason, an audit entry and a fresh
final review. The renderer never chooses; it only reports. Presenting that
decision in the dashboard is WP-5B's work.

---

## Related

* `assets/almanya24/rights/README.md` — the release record and why it lives
  outside the repository.
* `docs/plans/almanya24-avatar-iii-recovery.md` — the work-package checklist.
