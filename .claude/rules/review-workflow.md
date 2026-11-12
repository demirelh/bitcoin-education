---
paths:
  - btcedu/core/reviewer.py
  - btcedu/core/pipeline.py
  - btcedu/models/review.py
  - btcedu/models/review_item.py
  - btcedu/web/api.py
---

# Review Workflow Rules

- v2 gates, in order:
  - review_gate_transcript_qa (after transcript_qa) — may be adjudicated by an independent model
  - review_gate_1 (after CORRECTED) — transcript correction diffs
  - review_gate_2 (after ADAPTED; `review_gate_translate` when adapt is skipped) — may be adjudicated
  - review_gate_stock (after imagegen) — image selections
  - review_gate_anchor (after anchorgen) — presenter clips, only with `anchor.review_required`
  - review_gate_3 (after RENDERED) — final video plus fail-closed deterministic weather checks
  - a `publish` review task (created by `core/publisher.py`) guards the YouTube upload
- Profiles with `auto_approve_reviews: true` (tagesschau_tr) pass the editorial gates unattended
- Gates create ReviewTask records (status=PENDING) and pause the pipeline
- Approval: `btcedu review approve ID` or POST `/api/reviews/<id>/approve`
- Rejection with notes feeds back into re-processing
- Phase 5 granular review: ReviewItemAction + ReviewItemDecision for per-item accept/reject
- `has_approved_review()` / `has_pending_review()` control pipeline flow
