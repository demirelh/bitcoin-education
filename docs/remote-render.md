# Remote render on GitHub Actions

The render stage can run on a GitHub-hosted runner instead of the Raspberry Pi.
The local renderer stays fully functional and is both the fallback and the
manual alternative.

## Why

Rendering a ten-minute bulletin takes the Pi about 47 minutes and saturates all
four cores. That load is what starved systemd's watchdog ping and hard-reset the
machine on 2026-08-07 (`CPUQuota=300%` in `deploy/btcedu-run.service` fixed the
crash, not the duration). A hosted runner does the same work in roughly
19 minutes.

Measured on `ubuntu-latest` (`.github/workflows/render-benchmark.yml`):

| Measurement | Value |
| --- | --- |
| `raw_ffmpeg_encode` (60 s clip) | 28.5 s |
| `btcedu_create_segment` (60 s clip) | 112.8 s |
| projected for a 10-minute bulletin | 18.8 min |

Note that ~75 % of the render time goes into the overlay filter chain
(Ken Burns, animated lower thirds, colour correction), not into x264. Optimising
that chain would help locally *and* remotely.

## How it works

```
Pi                                  GitHub
--------------------------------    ------------------------------------
build_job_package()  ~33 MB
  job.json (episode + settings)
  episode/ (chapters, images,
            tts, render/inputs)
        |
        |  draft release asset
        v
                                    workflow_dispatch (render.yml)
                                      checkout branch
                                      verify commit == the Pi's HEAD
                                      apt: ffmpeg + Noto/Roboto fonts
                                      pip install -e .
                                      scripts/render_job.py
                                        -> render_video()   <-- same code
        ^
        |  run artifact  ~480 MB
        |
unpack_result()
finalize: ContentArtifact, MediaAsset,
          episode.status = RENDERED
```

There is deliberately **no second render implementation**. The runner rebuilds a
throwaway SQLite database with a single episode row and calls
`btcedu.core.renderer.render_video`, exactly what the Pi would have done.

### What is *not* transferred

- `render/segments/` and `render/draft.mp4` on the way out (they are outputs;
  ~850 MB of pointless upload)
- `render/segments/beats/` on the way back (~370 MB of scratch data the Pi
  never reads)
- Any credential. `render_settings_snapshot()` is a `render_*` prefix
  whitelist, and a test asserts that no secret can leak into the payload.
- The episode's source URL.

### Correctness guards

- **Same code**: the Pi refuses to dispatch when local `HEAD` differs from
  `origin/<branch>`, and the workflow aborts if the checked-out commit is not
  the one the Pi packed.
- **Same settings**: every `render_*` setting is shipped. This matters twice
  over, because those values also feed the `.render_settings` fingerprint — a
  missing one would make the Pi re-render forever.
- **Same fonts**: the workflow installs `fonts-noto-core` and
  `fonts-roboto-unhinted`, mirroring `deploy/setup-web.sh`.
- **Idempotency**: an already-current draft is detected *before* anything is
  uploaded.
- **Weather chapters** need no Chromium on the runner: `imagegen` has already
  rendered them into `images/chNN_weather.mp4`.

### Cleanup

The temporary draft release is deleted in a `finally` block, and the result
artifact is deleted right after download — a rendered episode is ~480 MB and
would otherwise fill the account's artifact storage quota.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `RENDER_EXECUTION_MODE` | `github` | Default target; the dashboard switch overrides it |
| `GITHUB_RENDER_REPO` | derived from `origin` | `owner/repo` |
| `GITHUB_RENDER_WORKFLOW` | `render.yml` | Workflow file name |
| `GITHUB_RENDER_TIMEOUT` | `5400` | Seconds to wait for the run |
| `GITHUB_RENDER_POLL_INTERVAL` | `20` | Seconds between status polls |
| `GITHUB_RENDER_FALLBACK_LOCAL` | `true` | Render locally when the offload fails |

`GITHUB_TOKEN` needs the `repo` and `workflow` scopes.

## Switching

- **Dashboard**: the switch in the top bar (`GET`/`POST /api/render-mode`).
  The choice is stored in the `app_settings` table and therefore survives a
  restart, unlike `.env`, which is only read at process start.
- **CLI**: `btcedu render --episode-id X --where github|local|auto`
  (`auto` follows the dashboard setting).

Dry-run always renders locally; there is nothing for a runner to do.

## Cost

A private repository consumes Actions minutes. At ~19 minutes per daily
bulletin that is roughly 570 minutes per month, which fits into the 2000
free minutes but leaves limited headroom for CI.

## Known limitations

- Partial re-rendering is lost: the runner starts from scratch because the
  existing segments are not uploaded. For a small change to a single chapter,
  a local render can be faster.
- The Pi still has to upload ~33 MB and download ~480 MB, so a slow uplink
  eats into the speedup.
