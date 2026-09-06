# Presenter release record

`btcedu anchor-readiness` refuses to let the ALMANYA24 presenter be generated
until it can mechanically answer six questions: is there consent, does it cover
voice and likeness, does it cover synthetic video, is it still valid, does it
cover this channel and territory, and has the operator signed off. This
directory is where it looks for the answers.

## What belongs here — and what does not

The record holds **statuses and one internal reference**. It does not hold the
contract, and it does not hold personal data. The schema enforces that: any key
outside the allow-list in `btcedu/core/anchor_rights.py` is rejected rather than
ignored, so a well-meaning "let's also note her address here" fails validation
instead of quietly landing in git.

- `anchor-rights.example.json` — committed, fictional, safe to read.
- `anchor-rights.json` — the real record. **Never committed**; `.gitignore`
  covers it. Deploy it with the same care as `.env`.

The contract itself stays wherever the operator's organisation keeps contracts.
`contract_reference` is the pointer to it — a file number, not a filename.

## Fields

| Field | Meaning |
|---|---|
| `schema_version` | Always `1` today. |
| `record_version`, `updated_at` | Bump when the record changes, so a stale copy is visible. |
| `presenter_rights_id` | Pseudonymous identifier. Not a name. |
| `consent_confirmed` | The presenter agreed. |
| `voice_likeness_confirmed` | Voice and likeness use is covered. |
| `synthetic_video_confirmed` | Synthetic video generation specifically is covered. |
| `permitted_channels` | Channels the release covers, matched against `anchor.rights.channel`. |
| `permitted_territories` | Territories, matched against `anchor.rights.territory`. `worldwide` matches everything. |
| `valid_from`, `valid_until` | ISO dates. `valid_until` may be `null` for open-ended. |
| `revoked`, `revoked_on` | Withdrawal. `revoked: true` blocks every further generation immediately. |
| `contract_reference` | Internal pointer to the externally stored contract. |
| `operator_approval` | `approved`, `approved_by_ref` (an initial or ticket id), `approved_on`. |
| `ai_disclosure_text` | The disclosure shown with the published video. Empty blocks. |

## Wiring

```yaml
stage_config:
  anchor:
    rights:
      record_file: /etc/btcedu/anchor-rights.json
      channel: almanya24-youtube
      territory: DE
```

An absolute path outside the working tree is the intended setup. A relative
path is resolved against the process's working directory and is only really
appropriate in tests.

## Why it fails closed

Every one of the six questions blocks when it is unanswered. A missing record,
an expired release, a channel the release does not name and an empty disclosure
text are all `BLOCKED`, not warnings. A synthetic presenter is somebody's face;
"we could not find the paperwork" is not a reason to broadcast it.
