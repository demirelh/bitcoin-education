# Review Gate Flow

## Overview

Review gates pause the pipeline at critical points so a human can approve or reject automated work before proceeding.

## Gate locations

| Gate | After status | Reviews what |
|------|-------------|-------------|
| review_gate_transcript_qa | CORRECTED | Blocking transcript uncertainty and verification conflicts |
| review_gate_1 | CORRECTED | Transcript correction diff |
| review_gate_2 | ADAPTED | Merged deterministic/LLM translation findings and constrained adaptation |
| review_gate_stock | CHAPTERIZED + imagegen | Stock image/video selections |
| review_gate_3 | RENDERED | Final rendered video |

The translation gate evaluates GREEN/YELLOW/RED before chapterize. GREEN
approves and hashes narration, YELLOW may run bounded targeted translate/adapt
repairs, and RED creates a review task. Critical deterministic findings cannot
be downgraded by the model.

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

- **APPROVED**: pipeline resumes only if the task still matches current artifact hashes
- **REJECTED**: episode stays at current status, needs manual intervention
- **CHANGES_REQUESTED**: reviewer notes are fed back into re-processing (e.g., corrector re-runs with feedback)

## Data artifacts

- Diffs: `data/outputs/{ep_id}/review/correction_diff.json`, `adaptation_diff.json`
- Review history: `data/outputs/{ep_id}/review/review_history.json` (append-only audit trail)
- Sidecars (Phase 5): `data/outputs/{ep_id}/review/script.adapted.reviewed.tr.md`
- Transcript QA: `data/outputs/{ep_id}/transcript/transcript_qa.json`
- Deterministic translation QA: `data/outputs/{ep_id}/translation_qa.json`
- Merged gate/audit trail: `data/outputs/{ep_id}/translation_quality_gate.json`

Finding status changes (`open`, `resolved`, `dismissed`) append history and do
not silently rewrite the translation. A fresh QA run recomputes the gate.

## Granular review (Phase 5)

Per-item accept/reject for corrections and adaptations:
- `ReviewItemAction` — individual items extracted from diff (id, char range, original/changed text)
- `ReviewItemDecision` — per-item decision (ACCEPT/REJECT)
- POST `/api/reviews/{id}/apply` — assembles sidecar from accepted/rejected items

## Auto-approve

Profiles may auto-approve ordinary review gates. Transcript RED findings and
the final publish approval remain explicit safety boundaries. For
`tagesschau_tr`, `auto_publish: false` means review approval never uploads by
itself.

## Key functions

- `btcedu/core/reviewer.py`: `create_review_task()`, `approve_review()`, `reject_review()`, `request_changes()`, `has_approved_review()`, `has_pending_review()`
- `btcedu/web/api.py`: `/api/reviews/*` endpoints
