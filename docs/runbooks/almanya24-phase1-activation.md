# ALMANYA24 presenter — Phase 1 activation

**Status: CODE READY — PRODUCTION BLOCKED UNTIL PHASE 1.**

Everything in this runbook needs an operator, an account, a camera or a
signature. None of it can be done by the pipeline, and none of it has been
done. Until it is, `anchor_enabled` stays `false` and the pipeline renders
exactly as it does today.

This is the single canonical document for the activation. If another note
disagrees with it, this one wins.

Read the whole thing before starting step 1. Several steps cost money and one
of them (the digital twin) involves another person's likeness.

---

## 0. What you are switching on

A HeyGen **Avatar III Digital Twin** of a real presenter, animated against the
already-approved ElevenLabs narration, composited into the fixed ALMANYA24
studio. The reporter stays invisible; only the anchor's own blocks are sent to
HeyGen. The original TTS remains the only audio in the master — HeyGen's audio
track is never used.

Cost model: `0.0167 USD/s`, capped at `7.00 USD` per episode by the profile and
again by the global `max_episode_cost_usd`. A normal bulletin needs 150–200 s of
presenter, so roughly **2.50–3.35 USD** per episode.

---

## 1. HeyGen account, twin and looks

### 1.1 Account and API access

1. Log in to HeyGen with the account that will own the presenter.
2. Confirm the plan includes **API access** and that a credit balance exists.
3. Create an API key. Do not paste it anywhere yet — see 1.9.

### 1.2 Create the female Digital Twin — with explicit consent

**Do not start this step without a signed consent from the presenter.** See
section 4.

1. Record the twin footage to HeyGen's current Digital Twin specification
   (their studio guide is authoritative for lighting, framing and duration).
2. Submit it and wait for HeyGen's own consent verification to complete.
3. Note the resulting **avatar / twin identifier**.

### 1.3 Confirm Avatar III is available to this account

1. In the HeyGen UI, open the twin and check that **Avatar III** is offered as
   an engine.
2. If only Avatar IV/V are offered, **stop**. The profile pins
   `engine: avatar_iii` deliberately; omitting it makes HeyGen default to
   Avatar IV, and silently falling back is not acceptable. Contact HeyGen
   before going further.

### 1.4 Create ten professional outfit looks

Ten, because the rotation spends all of them before any repeats — a viewer
should not see the same jacket two evenings running.

For each look:

* business-formal, news-appropriate;
* **no logos**, no wordmarks, no brand-identifiable garments;
* **no fine patterns** — pinstripes, herringbone, tight checks; they alias
  badly at 1080p and shimmer when the presenter moves;
* **no problematic accessories** — large earrings, reflective jewellery,
  glasses with strong reflections, scarves that cross the chin;
* neutral against the studio background (see 2) — nothing in the same hue as
  the desk or the monitor surround.

### 1.5 Test every look with the same short Turkish line

Use one identical line for all ten so the comparison is about the look and
nothing else. Suggested:

```
İyi akşamlar. Almanya24 haber bülteninde günün öne çıkan başlıklarıyla karşınızdayız.
```

For each look check: lip-sync on the Turkish, hands, hairline, jacket edges,
and — if you intend to use transparency — whether the matte is clean.

### 1.6 Record the look IDs

Write down the **look ID** HeyGen shows for each of the ten. These are what go
into the profile; the twin identifier alone is not enough.

### 1.7 Reject anything that failed 1.4 or 1.5

A look that shimmers or mattes badly will do so in every episode it is rotated
into. Re-create it rather than accepting it.

### 1.8 Configure the look pool

Edit `btcedu/profiles/tagesschau_tr.yaml`, `anchor.looks`. Replace each
`REPLACE_WITH_HEYGEN_LOOK_ID` with a real ID and set `active: true`:

```yaml
    looks:
      - name: look_01
        avatar_look_id: "<real HeyGen look id>"
        active: true
      # ... ten of them
```

`btcedu anchor-readiness` refuses to activate while a placeholder is still
active and rejects duplicate IDs.

### 1.9 Store the API key

Put it in `.env` only:

```
HEYGEN_API_KEY=...
```

Never in the profile, never in a commit, never in a log. `Settings` masks
credential fields in its own `repr` and `utils/secrets.install_log_redaction()`
strikes configured values out of every log record, but neither helps if the key
is written into a tracked file.

### 1.10 Re-check the price

The profile records:

```yaml
    cost_per_second_usd: 0.0167
    cost_source: "HeyGen API pricing list — Avatar III Digital Twin 720p/1080p, 1 USD/min"
    cost_checked_on: "2026-09-05"
```

**Check HeyGen's current pricing page yourself before activating.** If it has
moved, change `cost_per_second_usd`, `cost_source` and `cost_checked_on`
together. The rate is what every budget preflight is computed from; a stale
rate makes the 7 USD cap meaningless. 4K is priced differently and is not this
rate.

---

## 2. Studio assets

These are produced once, by a designer, and reused for every episode. The
pipeline never generates the studio.

Directory: `assets/almanya24/studio/` (the profile's `scene.studio.asset_dir`).
It currently holds `manifest.example.json` and a `README.md`; the real package
does not exist. `assets()` in `btcedu/core/studio_manifest.py` is the
authoritative list of files, and `parse_studio_manifest` the authoritative
schema — copy the example to `manifest.json` and fill it in.

### 2.1 The files

| Manifest field | Required | Format | Resolution | FPS | Alpha | Duration | Audio | Example path |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `background` | **yes** | PNG or MP4 | 1920×1080 | – / 25 | no | still, or ≥ 10 s | none | `plate/studio_wide.png` |
| `intro_asset` | no | MP4 (H.264) | 1920×1080 | 25 | no | 3–6 s | optional | `plate/studio_intro_swing.mp4` |
| `loop_asset` | no | MP4 (H.264) | 1920×1080 | 25 | no | ≥ 10 s, seamless | none | `plate/studio_loop.mp4` |
| `shadow_layer` | no | PNG | 1920×1080 | – | **yes** | – | – | `layers/presenter_shadow.png` |
| `foreground_layer` | no | PNG | 1920×1080 | – | **yes** | – | – | `layers/news_desk.png` |
| `occlusion_mask` | no | PNG (image only) | 1920×1080 | – | **yes** | – | – | `layers/desk_mask.png` |
| `fallback_display_media` | **yes in practice** | PNG or MP4 | 1920×1080 | – | no | – | – | `fallback/neutral_topic_card.png` |

`fallback_display_media` is formally optional in the parser but
`studio_readiness_problems` refuses a studio without it: a scene whose topic
medium is missing must still show something deliberate in the monitor.

Each asset entry is `{"path": ..., "kind": "image"|"video", "sha256": ...}`.
`kind` must match the extension; `path` may not leave the studio directory, and
symlinks are resolved before that containment check.

Geometry, all in frame coordinates and validated against the frame:

* `width`, `height`, `fps`, `alpha_mode` (`alpha_webm` or the opaque fallback);
* `display_zone` — the studio monitor: `rect`, optional `corners` (four points,
  for a wall that is not axis-aligned), `fit_mode`, `focus_point`,
  `presenter_free`, `z_order`. **This is where the current topic image or video
  is drawn** — the same asset the reporter scene later shows full-frame;
* `presenter` — `anchor_x`, `anchor_y`, `scale`, `z_order`. The presenter is
  drawn *behind* `foreground_layer`, which is what makes the desk occlude the
  lower body instead of the presenter floating in front of it;
* `logo_zone` — must lie inside the frame;
* `safe_areas` — `lower_third`, `ticker`, `subtitle`. `_validate_geometry`
  refuses layouts that would hide text or the presenter.

Also required: `studio_version` and `asset_version`. A `studio_version`
starting `0.0.0` is treated as a placeholder and blocks readiness, as does
`placeholder: true` on the manifest or on any asset.

**Branding, opening/closing cards, stings and fonts** are *not* part of the
studio package. They live in the profile's branding and render configuration
and are byte-bound by the render input inventory
(`docs/render-input-integrity.md`), not by this manifest. Supply an actual font
file rather than a bare name if you want the Pi and a remote runner to draw
identical glyphs — a name is resolved by whichever machine renders, which is
recorded in `render_environment.font.resolution`.

Design constraints that the code cannot check:

* nothing in the plate may resemble a specific broadcaster's set;
* no Bitcoin iconography anywhere in the ALMANYA24 appearance;
* keep the inside of the display zone free of glare in the background plate,
  or the topic media will look washed out;
* keep the outfit palette (section 1.4) away from the desk and monitor hues.

### 2.2 Hashing the assets

Never write a hash by hand.

1. **Inspect first.** Put the real files in place and confirm that every `path`
   in `manifest.json` names a file that exists and is non-empty.
2. **Produce the digests from the actual bytes**, for example:

   ```bash
   cd assets/almanya24/studio
   find . -type f ! -name 'manifest*.json' ! -name 'README.md' \
       -exec sha256sum {} +
   ```

3. **Update the manifest atomically** — write a new file and rename it over the
   old one, so a half-written manifest never exists. Set each asset's `sha256`,
   clear every `placeholder`, and set real `studio_version` / `asset_version`
   values.
4. **Read the diff** before committing anything: `git diff` on the manifest.
5. **Re-run readiness:**

   ```bash
   btcedu anchor-readiness --profile tagesschau_tr
   btcedu anchor-readiness --profile tagesschau_tr --studio-mode opaque_mp4
   ```

   `studio_readiness_problems` re-hashes every declared file and reports a
   mismatch, a missing file, an empty file, an unsafe path, a remaining
   placeholder and a placeholder version. Exit code 0 is ready, 1 warnings, 2
   blocked.

There is no `btcedu studio-hash` command. If you add one, it must follow this
same order — inspect, propose, diff, apply atomically, re-run readiness.

## 3. Rights and transparency

**No contract data goes into the repository.** The profile holds statuses and a
*reference*; the signed agreement and the release record live outside (see
`assets/almanya24/rights/README.md`). The policy itself is
`docs/implementation/almanya24-brand-rights.md`.

In `btcedu/profiles/tagesschau_tr.yaml`, `scene.rights`:

```yaml
    rights:
      consent_documented: true          # the signed consent exists
      consent_reference: "<external id>"  # a pointer, never the document
      permitted_channels: ["almanya24-youtube"]
      permitted_territories: ["DE"]
      revoked: false                    # true stops every further generation at once
      ai_disclosure_required: true
      record_file: "<path to the external release record>"
      channel: almanya24-youtube
      territory: DE
```

These fields **record whether the paperwork exists; they do not interpret it.**
Setting `consent_documented: true` without a signed consent is a false
statement by the operator, not a technical shortcut.

What the paperwork itself must cover, and what the fields above stand for:

| Requirement | Where it lands |
| --- | --- |
| written consent to a digital twin | `consent_documented`, `consent_reference` |
| voice and likeness — which are licensed, and for what | external contract |
| platforms | `permitted_channels`, checked against `channel` |
| territories | `permitted_territories`, checked against `territory` |
| term (start and end) | external contract; re-check before each renewal |
| revocation — how the presenter withdraws, and how fast you must comply | `revoked: true` is the immediate technical stop |
| operator sign-off | external record referenced by `record_file` |
| AI disclosure | `ai_disclosure_required` |

The release is checked against **this** channel and **this** territory, not
against consent "in general".

AI disclosure in practice: the video description states that the presenter is
AI-generated, and YouTube's "altered or synthetic content" declaration is set
on the upload. `ai_disclosure_required: true` makes its absence a blocker.

`record_file` is empty today, which is why `btcedu anchor-readiness` blocks.

## 4. Dashboard authentication

The dashboard can approve reviews, spend the avatar budget, reset a circuit
breaker and publish. It refuses to start unconfigured.

```bash
# 1. password hash (interactive; the password is never echoed or stored)
btcedu generate-password-hash

# 2. session secret, once
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then in `.env`:

```
WEB_AUTH_ENABLED=true
WEB_BIND_HOST=127.0.0.1
WEB_SESSION_SECRET=<the token above>
WEB_OPERATOR_USERNAME=<name>
WEB_OPERATOR_PASSWORD_HASH=<the hash above>
WEB_COOKIE_SECURE=true
```

* **Gunicorn binds loopback only.** Caddy terminates TLS and proxies to it. If
  `WEB_BIND_HOST` is anything but loopback, `WEB_AUTH_ENABLED=false` is a fatal
  configuration error and the service will not start.
* `WEB_COOKIE_SECURE=true` means the session cookie never travels over plain
  HTTP — so the Caddy site must be HTTPS.
* Test: open the dashboard, confirm you are redirected to the login, log in,
  confirm **Logout is a POST** (it is a form button, not a link) and that it
  ends the session.

---

## 5. YouTube

* Target: `youtube.publish_target: test` → `default_privacy: private`.
* Use a **test channel**, not the production one, for the whole pilot.
* Verify the channel ID the OAuth credentials actually resolve to before the
  first upload — an OAuth file from another project uploads to another channel.
* OAuth files: `data/client_secret.json` and the token file, both outside git.
* `auto_publish: false` stays false. Publishing is a deliberate operator act.
* There is no production fallback: if the test target is misconfigured the
  upload fails rather than landing somewhere else.

---

## 6. Activation order

Do these in order. Do not reorder them; several steps exist to make the next
one safe.

1. **Back up the database and the configuration.** `data/` and `.env`, to
   somewhere off the Pi.
2. **Install the code.** `./run.sh` (refuses a dirty tree).
3. **Install dependencies.** Handled by `run.sh`; `pip install -e ".[dev,web]"`
   and `".[youtube]"` if uploading.
4. **Run migrations.** `btcedu migrate`. On a fresh database this now completes
   from empty; on an existing one it only adds what is missing and touches no
   existing episode, cost or review row.
5. **Configure dashboard auth** (section 4).
6. **Start the services.** systemd units + Caddy.
7. **Check auth and health.** `/health` responds without a session and says
   nothing operational; every other route redirects to the login.
8. **Place the studio assets** (section 2).
9. **Generate the studio hashes and verify the manifest** (section 2.2).
10. **Configure the rights and transparency block** (section 3).
11. **Enter the ten look IDs** (section 1.8).
12. **`btcedu anchor-readiness`** — offline. Must be green.
13. **`btcedu anchor-readiness --online`** — reaches HeyGen read-only,
    confirms the account, the engine and the looks exist.
14. **`anchor_enabled` stays `false`.**
15. **`btcedu smoke-test-almanya24`** — free, no provider calls, end to end.
16. **One explicitly confirmed real HeyGen test of ~10 s** (section 7).
17. **Check the alpha WebM** — matte, edges, hair.
18. **If alpha is unusable, check the opaque MP4 fallback** and set
    `output_format: mp4`, `studio_mode` accordingly.
19. **Only now set `ANCHOR_ENABLED=true`.**
20. **One complete private pilot episode.**
21. **Anchor review** — a person looks at the presenter clips.
22. **Final render.**
23. **Final review** — the deterministic checks plus a person (section 8).
24. **Upload to the private test channel.**
25. **Three private episodes on three different days.** Different topics,
    different looks, different weather.
26. **Only then** make a separate production decision.

---

## 7. The paid 10-second acceptance test

The one deliberately billable step. It is started by hand and confirmed
explicitly; nothing in the pipeline triggers it.

**Before it runs, the command shows and you confirm:**

* the chosen look (name and ID);
* the exact text to be spoken;
* the expected duration;
* the estimated cost at the configured rate;
* the engine (`avatar_iii`) and avatar type (`digital_twin`);
* the output format (`webm` / `mp4`);
* the studio mode;
* an explicit yes/no.

**Afterwards, check:**

| What | Why |
| --- | --- |
| actual cost | must match the estimate; if not, the rate in the profile is wrong |
| provider job ID | recorded, so the job can be reconciled and never bought twice |
| lip-sync | on Turkish specifically |
| Turkish pronunciation | German names are the usual failure |
| transparency / matting | hair and hands are where it breaks |
| clothing | no shimmer, no logo, no aliasing |
| hands and face | no melting fingers, no drifting eyeline |
| edge artefacts | halo around the silhouette |
| body position | inside the studio's safe area, correctly occluded by the desk |
| studio integration | scale and eyeline match the set |
| topic monitor | the current topic asset is in the display, undistorted |
| original TTS | the master carries the ElevenLabs track, **not** HeyGen audio |
| audio/video duration | the clip matches the narration part |
| ffprobe | codec, pixel format, alpha channel, frame rate as expected |
| dashboard preview | the clip plays in the review UI |
| reconciliation / retry | kill the run mid-flight; the job must be resumed, not re-bought |

**None of this is executed by WP-8C.** It is written down so it can be.

---

## 8. Visual pilot acceptance checklist

Go through this for each of the three pilot episodes, by eye.

* [ ] the same presenter in every scene
* [ ] the same outfit throughout the episode
* [ ] no outfit, face or size change between chapters
* [ ] consistent lighting across scenes
* [ ] clean matting, no background bleed
* [ ] no flicker at hair or hands
* [ ] believable eye contact with the camera
* [ ] calm, news-appropriate gestures
* [ ] no visible cut jump between anchor scenes
* [ ] the correct topic image in the studio monitor
* [ ] nothing important in the picture hidden by desk, ticker or lower third
* [ ] the reporter's asset shown correctly full-frame
* [ ] lower third correct (name, spelling, position)
* [ ] ticker correct
* [ ] subtitles correct and in sync
* [ ] weather handover correct
* [ ] opening and closing correct
* [ ] **no Bitcoin elements anywhere in the visible ALMANYA24 appearance**
* [ ] no resemblance to a specific broadcaster's design
* [ ] AI disclosure present, on-screen and in the description
* [ ] sources and media rights checked for every asset used
* [ ] audio level and loudness professional (−15 LUFS narration bed)

---

## 9. Rollback

If anything goes wrong, this is the whole of it:

1. **Set `ANCHOR_ENABLED=false`** and restart. The pipeline returns to the
   render path that worked before.
2. **Do not forget the jobs already in flight, and do not re-buy them.** They
   are in `avatar_jobs` with their provider job IDs.
3. **Keep reconciling provider jobs**: `btcedu avatar-reconcile`. A job that
   was paid for must be accounted for even if you never use the clip.
4. **Preserve the costs.** Do not delete `avatar_jobs` rows to tidy up; the
   cost history is the audit trail.
5. **Do not reset the database. Do not reverse a migration.** The new tables
   are inert when the feature is off.
6. **Keep existing artefacts**, or move them to a quarantine directory. Do not
   delete them — a clip you paid for is evidence.
7. **Voice-over override is per episode and deliberate.** It is not a global
   switch and must not be used to paper over a systemic failure.
8. **YouTube publishing stays manual.** Nothing auto-publishes during or after
   a rollback.
9. **Re-enable only after `btcedu anchor-readiness` is green again**, offline
   and online, plus a fresh free smoke test.

---

## 10. Operator actions, in one list

Everything below needs a human. None of it is done.

1. HeyGen account with API access and credit.
2. Signed consent from the presenter.
3. Digital Twin created and verified by HeyGen.
4. Avatar III confirmed available for the account.
5. Ten outfit looks created, tested and accepted.
6. Ten look IDs entered in the profile, `active: true`.
7. HeyGen API key in `.env`.
8. Price re-checked against HeyGen's current page.
9. Studio assets produced and placed.
10. Studio hashes generated and the manifest verified.
11. Rights and transparency block configured, contract held externally.
12. Dashboard password hash, session secret, HTTPS via Caddy.
13. YouTube test channel and OAuth files, channel ID verified.
14. `anchor-readiness` green offline and online.
15. The paid 10-second acceptance test, confirmed by hand.
16. Alpha-vs-MP4 decision made.
17. `ANCHOR_ENABLED=true`.
18. Three private pilot episodes on three different days, reviewed by eye.
19. A separate, explicit production decision.
