---
paths:
  - btcedu/core/reviewer.py
  - btcedu/core/pipeline.py
  - btcedu/models/review.py
  - btcedu/models/review_item.py
  - btcedu/web/api.py
---

# Review Workflow Rules

- v2 pipeline has 3 review gates plus review_gate_stock:
  - review_gate_1 (after CORRECTED) — transcript correction diffs
  - review_gate_2 (after ADAPTED) — cultural adaptation diffs
  - review_gate_3 (after RENDERED) — final video
  - review_gate_stock (after imagegen) — stock image/video selections
- Gates create ReviewTask records (status=PENDING) and pause the pipeline
- Approval: `btcedu review approve ID` or POST `/api/reviews/<id>/approve`
- Rejection with notes feeds back into re-processing
- Phase 5 granular review: ReviewItemAction + ReviewItemDecision for per-item accept/reject
- `has_approved_review()` / `has_pending_review()` control pipeline flow
