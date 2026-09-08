# ALMANYA24 Avatar III — release readiness

**Verdict: CODE READY — PRODUCTION BLOCKED UNTIL PHASE 1.**

The implementation is complete and tested. It cannot go into production,
because everything that remains needs an account, an asset, a signature or a
person — see `docs/runbooks/almanya24-phase1-activation.md`, which is the
single canonical activation document.

Nothing in this repository has ever called HeyGen. `ANCHOR_ENABLED` is `false`
and stays false.

---

## 1. Scope implemented

| Package | What it delivered |
| --- | --- |
| WP-1 | Scene plan, persistent presenter assignment, avatar job register, migration 016 |
| WP-2 | `generate_anchors()` on the scene plan and the job register; only anchor scenes are ordered |
| WP-3 | Studio manifest, compositor, media resolution |
| WP-4 | Scene renderer, remote render contract |
| WP-5A | `anchor-readiness`, rights contract, reconciliation, failure policy |
| WP-5B | Avatar review gate, digest binding, regeneration model, dashboard API |
| WP-5C | Coordinator with bounded concurrency, retry matrix, idempotency window, circuit breaker, migration 019 |
| WP-6 | Synthetic end-to-end dry run, `btcedu smoke-test-almanya24` |
| WP-6A | Byte-bound clip integrity across seven trust boundaries, migration 020 |
| WP-7 | Dashboard runtime view, reconciliation controls, voice-over override |
| WP-8A | Flask-Login / Flask-WTF, fail-closed start-up, trustworthy operator identity |
| WP-8B | Logout hardening; every render input byte-bound across five boundaries |
| WP-8C | Legacy-manifest audit, render input guard, system-input provenance, migration evidence, this handover |

## 2. Architecture

```
scene_planner ──> anchor scenes only ──> avatar_coordinator ──> HeyGen (Avatar III)
                                              │
                     persistent avatar_jobs ──┘   provider job id, idempotency key,
                                                  reserved / submitted / completed
                                                  clip bound to its bytes
scene_renderer ──> studio composite ──> renderer.render_video ──> draft.mp4
     │                    │                       │
  studio_manifest    topic media in the      render_inputs (SHA-256 of every file)
  (hashed assets)    display zone            render_guard (what ffmpeg opened)
                                             render_environment (which machine)
```

Audio: the ElevenLabs TTS speaker parts are the only audio in the master.
HeyGen's own audio track is never used.

## 3. Security model

* Dashboard closed by default; the service refuses to start unconfigured, and a
  non-loopback bind with authentication disabled is a fatal configuration error.
* Session cookie `Secure`; HTTPS terminated by Caddy, Gunicorn on loopback.
* Every route authenticated except three explicitly public endpoints
  (`auth.login`, `static`, `api.health`).
* Logout is an authenticated, CSRF-protected POST. No state-changing GET.
* Operator identity is server-side; it cannot be forged from the client.
* Credentials come from `.env` only. `Settings` masks them in its `repr` and
  `utils/secrets.install_log_redaction()` strikes them out of every log record.
* The remote render package applies a secret-name denylist both when packing
  and when unpacking.
* No provider action can be triggered from the renderer or from a web request.

## 4. Cost model

| Item | Value | Source |
| --- | --- | --- |
| Rate | `0.0167 USD/s` | HeyGen API pricing list, Avatar III Digital Twin 720p/1080p, 1 USD/min |
| Checked | `2026-09-05` | `anchor.cost_checked_on` in the profile |
| Stage cap | `7.00 USD` | `anchor.max_cost_usd` |
| Episode cap | `max_episode_cost_usd` | global |
| Concurrency | 3 | `anchor.max_concurrent_jobs` |
| Typical episode | 150–200 s → ≈ 2.50–3.35 USD | measured in the synthetic run |

Reserved, submitted and unresolved jobs all count against the budget before a
new one is bought. **The rate must be re-checked by hand before activation**
(runbook 1.10); it is configuration, not a constant.

## 5. Test evidence

See section 10 for the numbers from this session's final run.

Targeted suites: avatar jobs, scene planner, anchor generator, anchor service,
concurrency, coordinator, studio manifest, scene renderer, renderer, render
inputs, render input boundaries, render input legacy, render guard, render
environment, remote render, publisher, review gates, web/auth/CSRF, avatar
operations, migrations, ALMANYA24 end-to-end.

Every external provider is mocked. No test has ever made a real API call.

## 6. Migration evidence

`tests/test_migrations_phase1.py`:

* a fresh database — the real deployment path (`init_db` → `create_all` →
  migrations) — applies all migrations and produces every avatar table, the
  columns the code reads and the indexes the hot lookups need;
* an existing pre-avatar database upgrades, keeps its episodes, its costs and
  its publish state, and gains nothing it should not (`avatar_jobs`,
  `presenter_assignments`, `avatar_regeneration_requests` are all empty
  afterwards — migrating never manufactures an approval or a job);
* repeated runs are idempotent (checked three times, because `run.sh` migrates
  on every deploy);
* a migration that fails halfway leaves its version unrecorded, names itself in
  the diagnosis, and completes on retry.

**A real bug was found and fixed here.** Migration 001 inserted the default
channel naming only the columns that existed when it was written. On a fresh
database `create_all` has already produced `channels.content_profile` as NOT
NULL, so the insert failed and `btcedu migrate` could not get past its first
migration on a brand-new installation — which is activation step 4.

## 7. Smoke test evidence

`btcedu smoke-test-almanya24` runs a whole synthetic episode with no provider
calls and no cost:

```
review_gate_anchor stops: awaiting avatar review
review_gate_anchor passes: digest 1e27af16cbb8…
render: 11 segments, 43.1s, 1.4MB [local]
review_gate_3 stops: video review task created
review_gate_3 passes: video review approved, episode marked APPROVED
publish: https://youtu.be/SMOKE-VIDEO-1
presenter clips ordered : 10
simulated avatar cost   : $0.2505 (not billed)
simulated uploads       : 1 [private]
```

## 8. Integrity contract

Three layers, in increasing suspicion:

1. **`render_inputs`** — SHA-256 over the bytes of every local file the video
   is made of, with size, kind and provenance, recorded relative to a named
   root so the set means the same thing on the Pi and on a runner. Verified at
   render, remote pack, remote take-back, review gate 3 and publish.
2. **`render_guard`** — every ffmpeg call in the protected render is read back
   and each file it opens must be in the inventory, under this render's own
   working directory, or under a declared machine-local system root. Anything
   else stops the render. This is what stops a *new* kind of input from
   silently going unhashed.
3. **`render_environment`** — the machine itself: ffmpeg/ffprobe versions, the
   relevant filters and codecs, the resolved font identity, renderer version
   and platform. Deliberately outside the content hash (or a remote render
   could never be current), and a remote result is explicitly relabelled so it
   can never read as locally produced.

**Legacy renders cannot be published.** A render made before WP-8B carries no
input digest; the digest is folded into the render content hash, so such a
render is always stale, `publisher._check_render_valid` fails and
`publish_video` raises. It must be re-rendered under the contract and
re-approved — an old approval does not carry over. Deleting the block from a
*current* manifest is not a bypass either, because the hash is recomputed from
the files. Proved by `tests/test_render_input_legacy.py`.

## 9. Requirement status

Legend: **A** implemented and automatically tested · **B** implemented, only
verifiable with real Phase-1 assets · **C** deliberately blocked pending
operator action · **D** not required, with reason.

### Presenter and outfit

| Requirement | Status | Evidence |
| --- | --- | --- |
| HeyGen | A | `anchor.provider: heygen`; service and tests |
| exactly Avatar III | A | `engine: avatar_iii` pinned; omitting it would default to Avatar IV, tested |
| Digital Twin | A | `avatar_type: digital_twin` |
| one outfit per episode | A | persistent presenter assignment; all anchor scenes share the look |
| same look ID for all anchor scenes | A | `test_avatar_jobs`, scene planner tests |
| rotation between episodes | A | `rotation_strategy: least_recently_used` |
| retry and restart keep the look | A | assignment is persisted, not recomputed |
| ten prepared look slots | C | ten slots exist, all `active: false` placeholders |
| manual look change only under the defined conditions | A | regeneration model, WP-5B |
| no Bitcoin content in the visible appearance | A | branding guard fails closed on any leak |

### Speakers and scenes

| Requirement | Status | Evidence |
| --- | --- | --- |
| anchor opens | A | scene planner |
| anchor announces every item | A | scene planner |
| reporter stays invisible | A | no avatar job is created for `reporter_male` |
| anchor → reporter → optional anchor | A | scene planner tests |
| weather handover | A | scene planner; weather scenes create no avatar job |
| closing | A | scene planner |
| only anchor blocks go to HeyGen | A | WP-2 tests |
| reporter/weather without an avatar order | A | WP-2 tests |
| original TTS is the only final audio | A | renderer uses the TTS speaker parts |
| no HeyGen audio in the master | A | same |

### Studio

| Requirement | Status | Evidence |
| --- | --- | --- |
| standardised studio | A | `studio_manifest`, schema-validated |
| reusable swing-in | A | `intro_asset` |
| topic image/video professionally in the monitor | B | `display_zone` implemented; needs the real plate |
| the same asset full-frame for the reporter | A | scene renderer |
| alpha WebM preferred | A | `output_format: webm` |
| safe opaque MP4 fallback | A | `--studio-mode opaque_mp4`, readiness decides |
| desk/occlusion, masks, safe areas | A | `foreground_layer`, `occlusion_mask`, `safe_areas`, geometry validation |
| ALMANYA24 branding | B | needs the real logo and plate |
| no broadcaster copy | C | a design judgement; runbook checklist |
| no generative re-creation of the studio per episode | A | the studio is a fixed hashed package |

### Operations

| Requirement | Status | Evidence |
| --- | --- | --- |
| persistent jobs | A | `avatar_jobs`, migration 016 |
| no double purchase | A | existing provider job IDs are polled, never re-ordered |
| idempotency key | A | 24-hour window |
| reconciliation | A | `btcedu avatar-reconcile`; `reconcile_required` fails closed |
| concurrency 3 | A | `max_concurrent_jobs` |
| retry/backoff | A | retry matrix, WP-5C |
| circuit breaker | A | `btcedu avatar-breaker` |
| budget cap 7 USD | A | preflight counts reserved/submitted/unresolved |
| readiness offline/online | A | `btcedu anchor-readiness` |
| anchor review gate | A | `review_required: true`, digest-bound |
| final review | A | `review_gate_3`, deterministic checks, fail-closed |
| private YouTube test operation | A | `publish_target: test` → `private` |
| no auto-publish | A | `auto_publish: false` |
| voice-over override manual only | A | per episode, WP-7 |
| remote render | A | integrity contract enforced on take-back |
| restart/recovery | A | resumable from every durable status |
| dashboard operation | A | WP-7 |
| authentication and CSRF | A | WP-8A/8B |

## 10. Test numbers

Filled in from the final verification run; see the session report for the exact
figures and timings.

## 11. Residual risks

1. **The presenter's visual quality is unverified.** Everything about matting,
   lip-sync on Turkish, hands and hair can only be judged on real HeyGen
   output. The 10-second acceptance test (runbook 7) exists for this.
2. **The price is a configured constant.** It was checked on 2026-09-05. If
   HeyGen changed it, every budget preflight is wrong until the profile is
   updated. Re-check by hand before activation.
3. **HeyGen's concurrency limit for this account is unknown.** Three is a
   conservative guess. Exceeding a real limit would surface as 429s on paid
   calls.
4. **TOCTOU.** Integrity is measured at the moment of each check. A file
   swapped between the publish check and ffmpeg's read is outside what any
   in-process hash can cover; the mitigation is filesystem permissions.
5. **`system` inputs are machine-local.** A font difference between the Pi and
   a runner is recorded and reported, not prevented.
6. **The studio package does not exist.** Readiness blocks on it, so this is
   contained, but it is the largest single piece of outstanding work.
7. **Rights are unresolved.** `consent_documented: false`, `record_file: ""`.
   The technical gates are honest only if the operator fills them in honestly.
8. **`btcedu migrate` had never been run against a truly fresh database.** It
   is fixed and tested now; the class of bug (a migration written against an
   older schema than `create_all` produces) can recur with the next migration.

## 12. Go / No-Go

| Gate | Verdict |
| --- | --- |
| Code complete | **GO** |
| Automated tests | **GO** — full suite green |
| Security model | **GO** |
| Migrations | **GO** — fresh, upgrade, idempotent, partial failure |
| Integrity contract | **GO** |
| Cost controls | **GO** in code; price re-check outstanding |
| HeyGen account, twin, looks | **NO-GO** — Phase 1 |
| Studio assets | **NO-GO** — Phase 1 |
| Rights and consent | **NO-GO** — Phase 1 |
| Real presenter quality | **NO-GO** — needs the paid acceptance test |
| Production | **NO-GO** |

**CODE READY. PRODUCTION BLOCKED UNTIL PHASE 1.**
