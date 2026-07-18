# Runbook: Handling Review Gates

## Finding pending reviews

```bash
# CLI
btcedu review list --status pending

# Web dashboard
# Navigate to Reviews tab — shows pending tasks with diffs
```

## Reviewing a correction (review_gate_1)

1. Open the diff: `data/outputs/{ep_id}/review/correction_diff.json`
2. Each item shows original text, corrected text, and character range
3. In the web dashboard, you can accept/reject individual corrections (Phase 5)

## Reviewing an adaptation (review_gate_2)

1. Inspect `translation_qa.json` and `translation_quality_gate.json`.
2. Review findings by story, severity, source/target excerpt, and required action.
3. Check retry generation, provider/model, cost, and unresolved status.
4. Use **Restart Translate + Adapt** for structured targeted feedback or
   **Restart All** for a fresh gate evaluation.

## Transcript QA

`transcript/transcript_qa.json` contains segment timestamps, primary/secondary
text, corrected text, uncertainty category, and blocking status. The QA tab can
approve the current artifact or request changes. Approval is rejected if the
artifact changed after the task was created.

## Approving a review

```bash
# Approve — pipeline resumes from next stage
btcedu review approve REVIEW_ID --notes "Reviewed, looks good"

# Via API
curl -X POST http://localhost:8091/api/reviews/REVIEW_ID/approve \
  -H "Content-Type: application/json" \
  -d '{"notes": "Approved"}'
```

## Rejecting a review

```bash
# Reject — episode stays at current status
btcedu review reject REVIEW_ID --notes "Chapter 3 adaptation is incorrect"

# Request changes — reviewer notes fed back into re-processing
btcedu review request-changes REVIEW_ID --notes "Fix the Bitcoin mining terminology"
```

When changes are requested, the corrector/adapter re-runs with reviewer feedback injected into the prompt.

For QA findings, changing a finding to resolved/dismissed records an audit
event. It does not itself turn RED into GREEN; rerun QA afterward.

## Applying granular review (Phase 5)

```bash
# Via API — applies per-item decisions and creates sidecar file
curl -X POST http://localhost:8091/api/reviews/REVIEW_ID/apply \
  -H "Content-Type: application/json" \
  -d '{"decisions": {"item-0000": "accept", "item-0001": "reject"}}'
```

Creates sidecar at: `data/outputs/{ep_id}/review/script.adapted.reviewed.tr.md`

## Unblocking a stuck pipeline

If the pipeline is stuck at a review gate:
1. `btcedu review list --status pending` — find the blocking review
2. Inspect the diff file
3. Approve or reject the review
4. Pipeline resumes on next `run-pending` cycle

Before publishing, confirm the final render review is bound to the current
render, metadata, and narration hash. `tagesschau_tr` always requires this
manual final approval.
