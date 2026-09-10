# ALMANYA24 — diagnosis of the first live editorial run

Run of 2026-09-10, `gpt-4o`, 64 model calls, 0.550098 USD, **0 of 8 stories
published**. This note records why, which rejections were justified, which were
defects, and what was changed.

All findings below were reconstructed from the durable artefacts of that run —
`data/almanya24-preview/current-news.sqlite`, the eight fetched source
documents and `current-preview-manifest.json`. **No further model calls were
made for this diagnosis.**

## What the run actually recorded

| Table / field | Value |
| --- | --- |
| `news_topics` | 8, all still `draft` |
| `news_claims` / `news_claim_revisions` | 38 |
| `news_claim_assessments` | 21 — 18 `supported`, 2 `unverifiable`, 1 `partial` |
| `news_evidence_links` | 23 |
| `news_article_revisions` | **0** |
| `news_provider_operations` | 50 `search` ok, 37 `llm` ok, 2 `article` not ok |

The research stage largely worked: **18 of 21 assessed claims were
`supported`**. Nothing failed for lack of evidence quality. The run died
between evidence linking and article drafting.

## Causal chain

```
evidence anchor gate rejects a passage that does support the claim
        └─> draft_story raises before an article is drafted            (5 stories)

article draft rejected by deterministic validation
        └─> provider operation left in status "submitted"
              └─> next attempt raises "requires reconciliation before retry"
                    └─> build harness treated that as fatal and stopped  (2 stories never tried)
```

Recorded block reasons (`current-preview-manifest.json`):

| Story | Reason | Verdict |
| --- | --- | --- |
| sweg-warnstreik | passage changes the claim's negation | defect |
| bundesweiter-warntag | passage does not contain the attribution | defect |
| berlin-krach-ermittlungen | passage changes the claim's negation | defect |
| bundestag-generaldebatte | passage does not contain the attribution | defect |
| grossbritannien-flugchaos | passage does not contain the claimed quote | defect |
| oelpreis-100-dollar | operation requires reconciliation before retry | defect (ledger) |
| turkiye-idlib, norwegen | never attempted | consequence of the abort |

None of the eight was a justified content rejection.

## Defect 1 — anchors compared raw text

`_validate_claim_anchors` compared claim fields against the passage with
`casefold()` and `in`. Extracted article bodies and model output disagree on
typography, not on content.

Proven against the real NDR document (`news_source_observations` id 2):

* claim attribution `Innenministerin Magdalena Finke (CDU)`
* real passage `… Innenministerin Magdalena Finke ( CDU ) …`

The fetched text pads brackets that carried inline markup, so the attribution
was "missing" although the passage names the person and the party. The same
class of mismatch affects typographic quotes (`„…“` vs `"…"`), soft hyphens
and en dashes.

**Fix:** `_anchor_text()` normalises both sides — NFKC, unified quotes and
dashes, collapsed whitespace, tightened brackets and punctuation — and is used
for every anchor comparison.

## Defect 2 — attribution required a composite spelling

The full string including a trailing annotation had to appear verbatim. A
passage naming `Bundesamt für Bevölkerungsschutz und Katastrophenhilfe` or
`Innenministerin Magdalena Finke` was rejected because it did not repeat the
`(BBK)` / `(CDU)` tag in that exact position.

**Fix:** `_attribution_forms()` accepts the attribution with a trailing
parenthetical annotation removed. The identity itself is still required
verbatim; only the annotation became optional.

## Defect 3 — negation compared whole passages

`_has_negation(claim.statement) != _has_negation(draft.passage)` treated any
negation word anywhere in a multi-sentence excerpt as contradicting the claim.

Real SWR passage: the anchoring sentence states the strike, a neighbouring
sentence says `Tübingen … sei nicht vom Warnstreik betroffen`. The passage was
rejected although it supports the claim exactly.

**Fix:** `_anchoring_sentences()` restricts the negation comparison to the
sentences that carry the claim (subject, number or attribution), falling back
to the whole passage when none can be identified. A passage that negates the
claim itself is still rejected — verified by test.

## Defect 4 — quote claims demanded reported speech verbatim

For `claim_type == "quote"` the **entire statement** had to be a substring of
the passage. The extracted claims are indirect speech
(`Ryanair-Betriebschef Neal McMahon forderte den Rücktritt …`), which can never
appear verbatim in the source. Every quote claim was therefore rejected
structurally.

**Fix:** `_quoted_spans()` derives the words a claim actually asserts verbatim
— text in quotation marks, otherwise direct speech after a colon. Pure reported
speech asserts no wording of its own and is carried by the subject and
attribution checks. A quote that is *not* in the passage is still rejected, and
so is a colon-introduced direct quote whose wording differs.

## Defect 5 — a judged reply was left in flight

In `generate_article_revision` the deterministic draft validation ran *outside*
the try block that owns the provider operation. `COMPLETED` was only ever set on
the happy path, so any content rejection left the row on `submitted` for good.
`reserve_provider_operation` then refused every later attempt with
`requires reconciliation before retry`, permanently poisoning that topic.

**Fix:** the validation moved into `_validate_draft_against_claims()` and is
called inside a guard that marks the operation `failed` with the rejection
reason and a `completed_at`. The reply had arrived and had been judged — that
is a decided outcome, not an uncertain one. `reconcile_required` remains
reserved for a genuinely unknown provider outcome.

The build harness additionally aborted the whole run on that message; it now
only stops for an exhausted budget.

## Verification (no model calls)

Offline replay of **all 38 stored claims** against the eight real source
documents, passages selected deterministically by token overlap:

| | accepted |
| --- | --- |
| gate before the fix | 17 / 38 |
| gate after the fix | 22 / 38 |
| **regressions** (accepted before, rejected now) | **0** |

The five newly accepted claims are exactly the reported-speech quote claims and
the bracket/typography cases.

Protection was re-checked explicitly and still rejects: a passage that negates
the claim, a fabricated or altered quote, a wrong number, an unnamed subject,
and an attribution that does not occur at all.

Regression tests added:
* `tests/test_editorial_research.py::test_supporting_evidence_accepts_passages_that_state_the_claim`
  — four parametrised cases, one per defect class.
* `tests/test_editorial_article.py::test_rejected_draft_closes_the_provider_operation_and_allows_a_retry`
  — asserts `failed` + `completed_at` + a successful redraft.

The pre-existing rejection tests were kept unchanged and still pass, including
the colon direct-speech case that must stay rejected.

## Remaining external blocker

The stages after evidence linking (article drafting, consistency check) cannot
be exercised end to end without paid model calls. The gate fixes are proven
offline; **that a full story now reaches publication is not yet demonstrated.**

## Bounded one-story live test (prepared, not executed)

Everything up to and including evidence linking is now proven offline. What is
**not** proven is that a story reaches a published article, because article
drafting and the consistency check need paid model calls.

The harness previously had only a global cost ceiling and no way to limit the
scope. It now takes explicit bounds:

```bash
cd /home/pi/AI-Startup-Lab/almanya24-newsroom-dev
PYTHONPATH=. .venv/bin/python data/almanya24-preview/build_current_preview.py \
    --only-story bundesweiter-warntag \
    --max-calls 12 \
    --budget-usd 0.25
```

| Bound | Value | Enforced by |
| --- | --- | --- |
| Provider / model | `openai` / `gpt-4o` | `PROVIDER` / `MODEL` in the harness |
| Stories | exactly 1 | `--only-story` |
| Model calls | 12 | `GlobalBudgetModel.max_calls` |
| Cost ceiling | 0.25 USD | `GlobalBudgetModel.budget_usd` |

Both guards were verified offline against a fake model: the call cap refuses the
call that would exceed it, and the cost guard refuses a call whose *maximum*
possible cost exceeds the remainder. Neither reaches the provider.

For scale: the failed run averaged 0.0086 USD per call over 64 calls. Twelve
calls for one story are therefore expected to stay well below the ceiling, but
the ceiling is what actually binds.

**This test has not been run** — it needs paid model calls and therefore an
explicit budget approval.

## Note — editable install does not see new subpackages

`btcedu.core.editorial` was added after `pip install -e .` was last run, and the
editable finder resolves from a static file map. Running a script whose
`sys.path[0]` is not the repository root therefore fails with
`No module named 'btcedu.core.editorial'`. Invoke the harness with
`PYTHONPATH=.` (as above) or re-run the editable install. The test suite is
unaffected because pytest puts the root on the path.

