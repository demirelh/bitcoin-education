# Review Gate Flow

## Overview

Review gates pause the pipeline at critical points so a human can approve or reject automated work before proceeding.

## Gate locations

| Gate | After status | Reviews what |
|------|-------------|-------------|
| review_gate_1 | CORRECTED | Transcript correction diff |
| review_gate_2 | ADAPTED | Cultural adaptation diff |
| review_gate_stock | CHAPTERIZED + imagegen | Stock image/video selections |
| review_gate_3 | RENDERED | Final rendered video |

## Flow

```
Pipeline reaches gate status
  -> has_approved_review()? -> YES -> skip gate, proceed
  -> has_pending_review()? -> YES -> return "review_pending", pause
  -> NO review exists -> create_review_task() -> return "review_pending", pause
```

## ReviewTask lifecycle

```
(created) PENDING -> IN_REVIEW -> APPROVED | REJECTED | CHANGES_REQUESTED
```

- **APPROVED**: pipeline resumes from next stage
- **REJECTED**: episode stays at current status, needs manual intervention
- **CHANGES_REQUESTED**: reviewer notes are fed back into re-processing (e.g., corrector re-runs with feedback)

## Data artifacts

- Diffs: `data/outputs/{ep_id}/review/correction_diff.json`, `adaptation_diff.json`
- Review history: `data/outputs/{ep_id}/review/review_history.json` (append-only audit trail)
- Sidecars (Phase 5): `data/outputs/{ep_id}/review/script.adapted.reviewed.tr.md`

## Granular review (Phase 5)

Per-item accept/reject for corrections and adaptations:
- `ReviewItemAction` — individual items extracted from diff (id, char range, original/changed text)
- `ReviewItemDecision` — per-item decision (ACCEPT/REJECT)
- POST `/api/reviews/{id}/apply` — assembles sidecar from accepted/rejected items

## Auto-approve

Corrections with <5 punctuation-only changes are auto-approved (MASTERPLAN section 9.4).

## Key functions

- `btcedu/core/reviewer.py`: `create_review_task()`, `approve_review()`, `reject_review()`, `request_changes()`, `has_approved_review()`, `has_pending_review()`
- `btcedu/web/api.py`: `/api/reviews/*` endpoints
