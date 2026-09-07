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
| Remote take-back | `remote_render._verify_returned_inputs` | refuses a result built from other bytes |
| Review gate 3 | `pipeline._run_stage("review_gate_3")` | fail-closed before the weather checks |
| Publish | `publisher._check_render_inputs` | a safety check like any other |

Remote take-back moves the runner's `system` entries to
`render_inputs.remote_system_inputs` (they are a fact about how the video was
drawn) and substitutes this machine's measurement, so every later boundary
compares local files against local files.

## Old manifests

* **No `render_inputs` block at all** — the render predates the contract. Every
  boundary passes and says so. There is nothing to compare, and refusing would
  strand finished episodes for a fault they cannot have committed.
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
* **The set is only as complete as the manifests.** A stage that starts writing
  a new kind of render input without recording it in a manifest will not be
  measured. `collect_extra_files` is the deliberate escape hatch for
  mode-dependent inputs.
* **`system` inputs are machine-local.** A font upgrade on the Pi is detected
  locally; a font difference between the Pi and a runner is caught only by
  name.
* **ffmpeg's own behaviour is not bound.** A different ffmpeg build can draw
  the same inputs differently; the binary is not a render input in this sense
  and is covered by the deployment, not by the manifest.
