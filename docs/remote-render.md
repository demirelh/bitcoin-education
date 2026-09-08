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
build_job_package()  ~30 MB
  job.json (episode + settings
            + expected hash/font)
  episode/ (chapters, images,
            tts, render/inputs)
  assets/  (profile audio that
            is not in git)
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

- `render/segments/`, `render/draft.mp4` and `render/draft_subtitled.mp4` on
  the way out (they are outputs; ~850 MB of pointless upload)
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
- **Same assets**: the audio a profile references (`intro_audio`,
  `topic_intro_audio`, `outro_audio`, `music_bed`) lives under `data/assets/`,
  which is git-ignored, so the runner has no copy. It travels with the job and
  is restored under the same relative path. Without this the intro jingle is
  silently missing from the video — this actually happened on the first
  successful offload.
- **Same fonts**: the workflow installs `fonts-noto-core` and
  `fonts-roboto-unhinted`, mirroring `deploy/setup-web.sh`. Because
  `find_font_path()` falls back to DejaVuSans-Bold with only a warning, the Pi
  additionally ships the font *file* it resolved and the runner refuses to
  render unless it resolves the same one. Comparing files rather than names
  means a Pi that is itself on the fallback still matches.
- **Same input hash**: the Pi sends the content hash it will validate the
  result against, and the runner recomputes it before rendering. This is the
  backstop for all of the above: anything the payload forgets to carry shows
  up here, and the job fails in seconds instead of returning a video the Pi
  would reject — which would put it in an endless re-render loop.
- **Idempotency**: an already-current draft is detected *before* anything is
  uploaded.
- **Weather chapters** need Chromium on the runner when animation is enabled:
  `imagegen` supplies the static weather card and structured data, then render
  creates `images/chNN_weather.mp4` from the actual TTS duration.

### Cleanup

The temporary draft release is deleted in a `finally` block, and the result
artifact is deleted right after download — a rendered episode is ~480 MB and
would otherwise fill the account's artifact storage quota.

### Where the work happens, and why not in `/tmp`

Each run gets a work directory under `data/outputs/.render-jobs/`, on the same
disk as the episode itself. It deliberately does **not** live in the system
temp directory: on the Raspberry Pi `/tmp` is a RAM-backed tmpfs of 3.9 GB,
while the returned result is ~800 MB and passes through the work directory
twice (downloaded archive, then extracted tree). Doing that in memory competed
with ffmpeg for the same 7.6 GB and pushed the machine into swap. Staging on
the episode's own filesystem has a second benefit: handing the files over
becomes a rename rather than a byte-for-byte copy.

For the same reason the artifact is streamed to disk in chunks instead of being
read through `response.content` — that call alone used to materialise the whole
body on the heap, and the `BytesIO` around it copied it a second time.

The work directory is removed in a `finally` block, but that cannot run when
the process is killed (Ctrl-C, a systemd stop, the OOM killer). Every remote
render therefore first deletes leftover directories older than six hours —
longer than a render takes, so a concurrent run is never disturbed.

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

The workflow itself runs with `permissions: contents: write`. That is not
cosmetic: a draft release is invisible to a token that only has
`contents: read`, so the job cannot download its own payload without it.

## Measured

One daily bulletin (8 chapters, 657 s of video):

| | Pi | GitHub runner |
| --- | --- | --- |
| Wall clock | ~47 min | ~18 min |
| Payload up | — | 30 MB in ~5 s |
| Result down | — | 462 MB |

The runner is only ~2.5x faster per frame; most of the render time is the
overlay filter chain (Ken Burns, animated lower thirds, colour correction),
not x264 encoding. The bigger win is that the Pi stays responsive — a
CPU-saturating local render is what triggered the watchdog reset that started
this work.

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
