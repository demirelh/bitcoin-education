# ALMANYA24 brand brief & presenter-rights matrix

> Operational policy, not legal advice. No rights are assumed, granted, or
> implied by this document. The canonical repo values come from
> `btcedu/profiles/tagesschau_tr.yaml`.

## 1) Canonical brand lock

| Field | Value | Binding use |
| --- | --- | --- |
| Display name | `ALMANYA24` | On-screen logo, lower thirds, intro/outro cards |
| Spoken name | `Almanya Yirmi Dört` | Spoken openings, closings, presenter references |
| Slogan | `Almanya'nın nabzı burada atıyor.` | Intro/outro card, promo copy, brand lockup |
| Source attribution | Hidden | Internal provenance only; never visible on screen |

Rules:

- Speak the `spoken_name`, not the raw logo string, when the line is meant to be read aloud.
- Use the `display_name` for graphics, cards, filenames, and metadata.
- Use the slogan as brand copy only; it must not replace source attribution.

## 2) Audience, tone, and visual identity

- **Audience:** Turkish-speaking viewers in Germany who want concise, locally relevant news.
- **Tone:** calm, credible, neutral, service-oriented, and non-sensational.
- **Editorial posture:** explain why a story matters; do not imitate a specific broadcaster or anchor.

| Element | Direction | Constraints |
| --- | --- | --- |
| Palette | News blue `#004B87`, white, charcoal, restrained neutrals | No broadcaster-copy palette, no loud accent colors |
| Typography | `Roboto-Condensed-Bold` for headline/brand use | No decorative faces, no script fonts, no ultra-light weights |
| Studio | Original, repeatable house set | No copied Tagesschau/ARD desk, framing, or motion language |
| Motion | Minimal and calm | No flashy wipes, no over-animated lower thirds |

## 3) Presenter decision framework

**Default branch: synthetic presenter.** Use it when the rights packet is incomplete or when the operator wants the lowest-risk production path.

**Licensed real presenter branch:** only if the operator can supply a signed release package and the logo/brand pack listed below.

| Condition | Branch | Notes |
| --- | --- | --- |
| Signed rights package, channel/territory scope, and logo pack are present | Licensed real presenter | Use only the operator-approved human presenter |
| Anything missing | Synthetic presenter | Keep the current synthetic branch; do not fabricate rights |

Implementation detail: the synthetic branch may use D-ID or another provider, but provider choice is not the brand decision.

## 4) Prohibited imitation

Do not intentionally imitate:

- Tagesschau / ARD studio geometry, desk, lighting, lower-third style, or music-bed feel
- Any specific presenter’s face, voice, gestures, cadence, or catchphrases
- Broadcaster logos, watermarks, or channel-name lookalikes
- Prompts that instruct the model to copy a named broadcaster or living presenter

## 5) AI disclosure

- Synthetic presenter: disclose AI-assisted presentation in the video description and/or a short on-screen card.
- Licensed real presenter: disclose any AI augmentation separately (voice cloning, face retargeting, synthetic set extension, translated lip-sync).
- The exact wording is operator-approved; this repository does not invent legal text.

## 6) Consent / license clauses the operator must provide

If the licensed real presenter branch is chosen, the signed packet must explicitly cover:

- use of name, voice, likeness, image, and performance
- permission for recording, editing, translation, dubbing, lip-sync, and derivative clips
- permission for commercial distribution
- exact channels and exact territories
- term, renewal, and scope
- no sublicensing unless explicitly granted
- no model training / no reuse outside this project unless explicitly granted
- right to revoke and the required deletion workflow

If any clause is missing, keep the synthetic branch.

## 7) Revocation and deletion process

If rights are revoked or narrowed:

1. Freeze new generation immediately.
2. Mark affected assets stale/locked.
3. Delete or quarantine raw uploads, cached derivatives, and biometric templates from project storage.
4. Stop reuse in prompts, render configs, descriptions, and archives where practical.
5. Request platform deletion where the platform supports it; if deletion is impossible, record the limitation and stop reuse.
6. Record revocation date, affected episode IDs, and operator confirmation.

## 8) Commercial territories and channels

The contract must list exact territories and exact channels. If a channel or territory is not written down, it is not licensed.

Typical channels to enumerate:

- main YouTube channel
- Shorts / Reels / social clips
- website embeds
- newsletter previews
- promo thumbnails / stills
- PR or press-kit stills

Typical territory fields to enumerate:

- Germany
- EU / EEA
- Switzerland
- Turkey
- worldwide digital distribution

## 9) Biometric data handling

Face images, voice recordings, lip-sync source files, and any derived embeddings are biometric / personal data.

Rules:

- store only the minimum required
- encrypt at rest and restrict access
- never use for unrelated model training
- never reuse outside the signed scope
- delete on revocation where required
- keep audit metadata separate from the raw biometric assets

## 10) Logo deliverables and validation sizes

Required deliverables:

- master wordmark (`SVG` + transparent `PNG`)
- square icon (`SVG` + transparent `PNG`)
- horizontal lockup (`SVG` + transparent `PNG`)
- light and dark variants
- end-card composition that fits the video frame

Validation sizes:

- `64x64`
- `128x128`
- `256x256`
- `512x512`
- `1024x1024`
- `1920x1080`

Acceptance rule: the logo must remain readable at the smallest sizes and unclipped at the largest video-frame size.

## 11) Operator sign-off checklist

- [ ] `display_name`, `spoken_name`, and slogan match the current profile
- [ ] synthetic vs licensed real presenter branch chosen
- [ ] if licensed: signed rights / release package is on file
- [ ] if licensed: territories and channels are explicitly listed
- [ ] AI disclosure text is approved
- [ ] logo pack is delivered in the required formats
- [ ] logo passes all validation sizes
- [ ] palette and typography are approved
- [ ] studio / set direction is approved and non-imitative
- [ ] biometric handling and retention rules are approved
- [ ] revocation / deletion workflow is documented
- [ ] internal provenance path remains separate from visible branding
- [ ] operator sign-off is recorded before publish

Until every required checkbox is true, keep the synthetic branch.
