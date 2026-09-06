# ALMANYA24 Studio Assets

This directory holds one *version* of the ALMANYA24 studio: the wall the
presenter stands in front of, the monitor that carries the evening's topic
media, the desk in front of her, and the geometry that says where all of it is.

**There is no `manifest.json` here, and that is deliberate.** The only file is
`manifest.example.json`. Asset phase 1 — producing the actual studio artwork —
has not happened. A `manifest.json` in this directory is read by the pipeline as
a statement that the studio exists; writing one now would make missing artwork
look like present artwork and would fail somewhere deep inside ffmpeg instead of
at the door.

## Making the studio real

1. Produce the assets listed in `manifest.example.json` at 1920×1080.
2. Copy the example to `manifest.json`.
3. Replace every `"placeholder": true` with the real value (or drop the flag).
4. Set `studio_version` and `asset_version` to something that is not `0.0.0-…`.
   Bump `studio_version` whenever the geometry changes and `asset_version`
   whenever only the artwork changes — the first invalidates compositing for
   every episode, the second only for episodes that get re-rendered.
5. Record each asset's `sha256`. The digest is what makes "the wall artwork was
   swapped" visible to the idempotency check.
6. Run the readiness check; it prints every remaining problem at once.

Until step 6 passes, `require_studio_ready()` raises and no studio render can
start. That is the intended behaviour.

## Fields

| Field | Meaning |
|---|---|
| `schema_version` | Manifest format. This build understands `1` only. |
| `studio_version` / `asset_version` | Feed the compositing hash; see above. |
| `width`, `height`, `fps` | The frame every scene is composited into. |
| `alpha_mode` | `alpha_webm` (transparent presenter over the plate) or `opaque_mp4` (the provider already baked in a studio). |
| `background` | The studio plate. Image or video. |
| `intro_asset` | Optional swing-in shot used by the opening template. |
| `loop_asset` | Optional moving plate for scenes that should not be a freeze frame. |
| `display_zone` | The monitor: `rect`, `fit_mode`, `focus_point`, optional four `corners` for a perspective fit, `presenter_free`. |
| `presenter` | `anchor_x`/`anchor_y` is the point the avatar clip's **bottom centre** is placed on, plus `scale`. |
| `shadow_layer` | Optional contact shadow drawn under the presenter. |
| `foreground_layer` | Optional desk or foreground drawn over the presenter. |
| `occlusion_mask` | Required in `opaque_mp4` mode when the display zone is *not* presenter-free. |
| `fallback_display_media` | Shown when a scene has no topic medium. Without it the studio is not ready. |
| `logo_zone` | Where the channel logo sits. |
| `safe_areas` | `lower_third`, `ticker`, `subtitle`. The display zone may not overlap any of them. |
| `provenance` | Free-form record of where the assets came from and what the rights situation is. |

## Rules the loader enforces

- Asset paths are relative and must resolve **inside this directory**. Absolute
  paths, `..` and symlinks pointing outside are refused.
- The display zone must lie inside the frame and must not overlap the lower
  third, the ticker or the subtitle band.
- In `opaque_mp4` mode the display zone must be `presenter_free`, or an
  `occlusion_mask` must be present. The opaque clip *is* the camera, so we do
  not know where the presenter is; drawing a monitor over her is the failure
  this rule exists to prevent.
- `corners`, if given, must be exactly four points in top-left, top-right,
  bottom-right, bottom-left order and inside the frame. They are only used when
  the local ffmpeg has the `perspective` filter; otherwise the monitor is filled
  as a plain rectangle and the result records that it fell back.

## What never happens here

No image or video is generated for the studio. The topic medium in the monitor
is always a file the existing pipeline already produced for that chapter — the
same file the reporter's blocks show full-frame. Compositing rearranges the
programme's media; it never adds to it.
