# ADR-004: Sidecar Files Instead of Overwriting Pipeline Artifacts

**Date:** 2026-03 (Phase 5)
**Status:** Accepted

## Context

When a reviewer applies granular changes (accepting some corrections, rejecting others), the result needs to be persisted. The question is whether to overwrite the original pipeline artifact or create a separate file.

## Decision

Review results are written as **sidecar files** in the `review/` subdirectory, never overwriting original pipeline artifacts.

Example: `script.adapted.tr.md` (original) -> `review/script.adapted.reviewed.tr.md` (sidecar)

## Rationale

- **Reproducibility**: original artifact is preserved exactly as the pipeline produced it
- **Audit trail**: sidecar + review decisions together show what changed and why
- **Safe re-runs**: re-running a pipeline stage regenerates the original artifact without losing review work
- **Diff clarity**: comparing original vs sidecar shows exactly which reviewer changes were applied

## Consequences

- Downstream stages (chapterize, imagegen, etc.) should prefer the sidecar if it exists, falling back to the original
- `data/outputs/{ep_id}/review/` directory contains all review artifacts
- `review_history.json` provides an append-only audit trail of all review decisions
