# Render input integrity (WP-8B)

Every local file the finished video is made of is measured, recorded and
re-checked at each lifecycle boundary. Before WP-8B a manifest named its inputs
by *path*; a picture could be replaced after the render and every later check
still reported the episode as current, because nothing had ever read the bytes.

## What is bound

`btcedu/core/render_input_collector.py` builds the set from the manifests the
render actually reads, not from a hand-kept list. It covers:

| Class | Key prefix | Source |
| --- | --- | --- |
| Studio plates, layers, masks | `studio:` | studio manifest |
| Opening/closing and topic cards | `studio:` (kind `card`) | studio manifest |
| Topic images | `media:` | `images/manifest.json` |
| Topic video clips | `video:` | `video/manifest.json` |
| Weather cards | `weather:` | `weather/manifest.json` |
| Chapter narration | `tts:` | `tts/manifest.json` |
| Per-speaker narration parts | `tts_part:` | `tts/manifest.json` metadata |
| Presenter (avatar) clips | `avatar:` | `anchor/manifest.json` |
| Intro/topic/outro stings | `sting:` | render config |
| Music bed | `music:` | render config / settings |
| Overlay font | `font:` | resolved render font |
| Templates, subtitles, override fallbacks | caller-chosen | `collect_extra_files` |

Each entry records `sha256` (streamed, 1 MiB chunks), `size_bytes`,
`media_type`, `kind`, `root`, a relative `path` and a short `provenance`
string. Where a manifest already *claims* a digest (studio assets, avatar
clips) the claim is kept as `declared_sha256` and compared, never trusted:
`declared_mismatches()` reports a manifest that disagrees with its own files.

## Roots, not absolute paths

Entries are relative to a named root — `episode`, `studio`, `assets` or
`system`. That makes the same recording valid on the Pi and on a GitHub
runner, and it makes containment checkable: `safe_resolve()` rejects absolute
paths, `..` segments and anything that resolves outside its root, including via
a symlink.

`system` (the font) is deliberately **excluded from the set digest**. The
digest feeds the render content hash, which the remote runner recomputes; the
runner's `fonts-noto` package is not the Pi's, so a shared font hash would
reject every remote render by construction. The font is still measured,
recorded and verified machine-locally, and the cross-machine protection stays
the existing font *name* comparison.

## Where it is checked

| Boundary | Function | Behaviour |
| --- | --- | --- |
| Render / staleness | `renderer.render_is_current` via the content hash | changed bytes make the render stale |
| Remote pack | `remote_render.build_job_package` | refuses to ship an incomplete set |
| Remote take-back | `remote_render._verify_returned_inputs` | refuses a result built from other bytes, and refuses a result carrying no set at all |
| Review gate 3 | `pipeline._run_stage("review_gate_3")` | fail-closed before the weather checks |
| Publish | `publisher._check_render_inputs` | a safety check like any other |

Remote take-back moves the runner's `system` entries to
`render_inputs.remote_system_inputs` (they are a fact about how the video was
drawn) and substitutes this machine's measurement, so every later boundary
compares local files against local files.

## Old manifests

* **No `render_inputs` block at all** — the render predates the contract. The
  per-boundary *input check* passes and says so: there is nothing to compare,
  and failing it would report a fault the episode cannot have committed.
  That leniency is never the last word (WP-8C). Because the input digest is
  folded into the render content hash, a legacy render is always stale against a
  current measurement, `render_is_current` reports *render inputs changed since
  last render*, `publisher._check_render_valid` fails and `publish_video`
  raises. A legacy render therefore cannot be published; it must be re-rendered
  under the contract and re-approved, and an old approval does not carry over.
  Deleting the block from a *current* manifest is not a bypass either — the
  content hash is recomputed from the files, not from the block.
  Proved by `tests/test_render_input_legacy.py`.
* **A remote result with no block** — refused. The runner executes our own code
  on the files we packed, so if this machine can measure a set, so could it.
  Only when the local measurement is also empty is the result accepted.
* **A block that exists but is incomplete** (an entry without `sha256`) —
  status `unrecorded`, and the boundary **refuses**. A half-written contract is
  a bug, not history.
* **A missing or changed file** — `missing` / `mismatch`, refused.
* **An unsafe path** — `unsafe`, refused.

## Invalidation

The input digest is part of `_compute_render_content_hash`, so every render
produced before WP-8B goes stale exactly once. That is the price of the hash
having been wrong until then. It is scoped:

* only the affected episode is invalidated — an unrelated episode with the same
  content is untouched;
* no provider work is triggered: avatar jobs are bound by their own contract
  (`core/avatar_integrity.py`) and a repainted picture never buys a clip;
* a rewrite with identical bytes changes nothing, so a touched mtime or a
  re-copied file does not cost a re-render.

## Residual risk

* **TOCTOU.** Verification measures the files at the moment of the check. A
  file swapped between the publish check and ffmpeg's read is outside what any
  in-process hash can cover; the window is short and the mitigation is
  filesystem permissions, not a second hash.
* **The set is only as complete as the manifests** — mitigated by the guard
  below, which turns a silent gap into a stopped render.
  `collect_extra_files` remains the deliberate escape hatch for
  mode-dependent inputs.
* **`system` inputs are machine-local.** A font upgrade on the Pi is detected
  locally; a font difference between the Pi and a runner is caught only by
  name.
* **ffmpeg's own behaviour is not bound.** A different ffmpeg build can draw
  the same inputs differently; the binary is not a render input in this sense
  and is covered by the deployment, not by the manifest.

## The guard: what the render actually opened (WP-8C)

The inventory above records what the pipeline *believes* it will use. Belief
and behaviour can drift — a stage learns to draw a new overlay, a filter grows
a `movie=` source, a config value points outside the episode — and none of that
would be visible in a digest of the files nobody thought to list.

`btcedu/core/render_guard.py` asks the other question. Every ffmpeg invocation
in the render path goes through one function,
`btcedu.services.ffmpeg_service._run_ffmpeg`, so the command can be read back
there and compared against the inventory before the process starts. No global
monkeypatching is involved; the single execution point is the abstraction.

Four classes are allowed and only four:

1. a file in the measured inventory;
2. a file under the render's own `<episode>/render/` working directory — an
   intermediate this render produced (levelled stings, built segments, the
   concat list), whose provenance is the render itself;
3. one exact path admitted with `admit_generated_input()` after the active
   render successfully generated it outside that directory. Timed weather
   videos use this because they are written beside their static cards only
   after the actual TTS duration is known; admitting the entire `images/`
   directory would weaken the inventory boundary;
4. a file under a declared system root (`DEFAULT_SYSTEM_ROOTS`: fonts, codec
   data), the documented machine-local class that cannot join a cross-machine
   digest. These are recorded in `system_inputs_seen()` rather than refused.

Anything else raises `UnknownRenderInputError` and the render stops.

Details worth knowing:

* **Files are recognised by asking the filesystem, not by syntax.** `-i` also
  takes lavfi descriptors (`anullsrc=`, `color=`), devices and `concat:`
  pseudo-paths; only `Path.is_file()` tells them apart. A path that does not
  exist is ffmpeg's error to report, not the guard's.
* **Indirect sources are followed**: concat/ffconcat lists are read and the
  files they name are checked too, and `fontfile=`, `movie=`, `amovie=` and
  `textfile=` are extracted from filter arguments with ffmpeg's `\:` escaping.
* **Arming is explicit and thread-local.** Only `render_video` arms it (the job
  manager renders in threads), and only when a measured set exists — an episode
  whose inputs could not be measured at all is already reported through the
  `missing` list, and refusing it here would turn a diagnosable state into an
  unexplained one. The smoke test, the weather stage and TTS levelling are
  untouched.
* **`disarmed()`** exists for probing an output the render just wrote.
* The weather renderer calls `subprocess.run` directly. Static cards are
  produced in imagegen; animated weather videos are produced during render
  from the actual TTS duration and their exact output path is admitted before
  ffmpeg consumes it as topic media.

Covered by `tests/test_render_guard.py`.

## System inputs and reproducibility (WP-8C)

Not everything that shapes a video is a file the pipeline owns. Which ffmpeg
built the frames, which encoder it picked, and which font file the name
`NotoSans-Bold` actually resolved to are properties of the *machine*, and two
runs from byte-identical inputs can differ in all of them.

`btcedu/core/render_environment.py` records them. Every render writes a
`render_environment` block into `render_manifest.json` and into the render
provenance:

| Field | Why |
| --- | --- |
| `renderer_version` | which build of this repository drew the video |
| `ffmpeg_version`, `ffprobe_version` | the tools that made and measured it |
| `filters`, `encoders`, `decoders` | only the names the render relies on — a full listing would be kilobytes of churn |
| `font` | `name`, `resolved` and whether it resolved to a **file** or was left to **fontconfig**; a bare name is resolved by whichever machine draws the frame |
| `platform` | `system`, `machine`, `python` only — no hostname, no user, no home directory |

The provenance additionally carries `system_inputs_seen`: the machine-local
files the render guard actually observed being opened.

**This is deliberately outside the content hash.** The hash has to mean the same
thing on the Pi and on a GitHub runner; folding the ffmpeg build into it would
make every remote render permanently stale, which is the opposite of what the
remote path exists for. The environment is evidence for the final review, not
an identity for the artefact. `tests/test_render_environment.py` asserts that
`_compute_render_content_hash` never learns about it.

### A remote result stays recognisable as remote

Taking a remote result back rebases its paths onto this machine. Without a
counter-measure the artefact would then be indistinguishable from a local
render. `remote_render._record_remote_origin` therefore:

* relabels the runner's own description as `origin: "remote"` and keeps it;
* adds this machine's description as `local_render_environment`;
* writes `render_environment_differences` — the reviewer is told what was
  different about the machine rather than being left to assume it was this one;
* sets `render_origin: "remote"` in both the manifest and the provenance.

A result that carries no description at all is still marked remote; it is never
allowed to read as local.
