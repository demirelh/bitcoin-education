# ADR-003: Granular Review with Normalized Tables

**Date:** 2026-03 (Phase 5)
**Status:** Accepted

## Context

Early review gates (Phases 1-2) used all-or-nothing approval: approve the entire correction or adaptation, or reject everything. This forced reviewers to accept minor issues or reject good work because of a few problems.

## Decision

Implement per-item review using normalized database tables:
- `ReviewItemAction` — individual items extracted from a diff (ID, char range, original text, changed text)
- `ReviewItemDecision` — per-item decision (ACCEPT or REJECT with notes)

## Rationale

- **Precision**: accept good changes, reject bad ones, without re-running the entire stage
- **Audit trail**: every per-item decision is recorded with timestamps
- **Sidecar output**: accepted/rejected items are assembled into a sidecar file (reviewed version) without modifying the original pipeline artifact
- **Normalized model**: separate action + decision tables allow querying across reviews

## Consequences

- Migration 007 creates `review_item_actions` and `review_item_decisions` tables
- POST `/api/reviews/{id}/apply` assembles sidecar from per-item decisions
- Sidecar written to `data/outputs/{ep_id}/review/script.adapted.reviewed.tr.md`
- Original `script.adapted.tr.md` is never modified (see ADR-004)
