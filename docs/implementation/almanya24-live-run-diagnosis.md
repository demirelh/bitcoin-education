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
/home/pi/.venvs/almanya24-newsroom-dev/bin/python \
    data/almanya24-preview/build_current_preview.py \
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

**This test has been run** under an explicit approval for the single story
`bundesweiter-warntag` with `openai/gpt-4o`, at most 12 provider calls and at
most 0.25 USD. The result is documented below.

## Correction — the import failure was not a stale editable finder

An earlier note in this document blamed a stale editable file map. That was
wrong, and the corrected finding matters because the original explanation would
have led to a `pip install -e .` that silently rebinds the **production**
interpreter.

`.venv` inside this worktree was a symlink to
`../bitcoin-education.old-20260709/.venv` — the very same environment the
production checkout uses through its own `.venv` symlink. That environment's
editable install maps `btcedu` to
`/home/pi/AI-Startup-Lab/bitcoin-education/btcedu`, i.e. to production code,
which does not contain the newsroom package at all:

```
$ cd /tmp && …/bitcoin-education.old-20260709/.venv/bin/python -c "import btcedu.core.editorial"
ModuleNotFoundError: No module named 'btcedu.core.editorial'
$ …/bitcoin-education.old-20260709/.venv/bin/python -c "import btcedu; print(btcedu.__file__)"
/home/pi/AI-Startup-Lab/bitcoin-education/btcedu/__init__.py
```

`PYTHONPATH=.` only masked this by pushing the development tree in front of a
production binding. Running `pip install -e .` against that shared environment
would have repointed production at this worktree — a production change, and
therefore out of scope.

A correctly installed development environment already existed:
`/home/pi/.venvs/almanya24-newsroom-dev` maps `btcedu` to this worktree, and it
is the environment the preview unit already uses. It must be invoked by its
absolute path:

```bash
/home/pi/.venvs/almanya24-newsroom-dev/bin/python data/almanya24-preview/build_current_preview.py
```

Repointing the worktree's `.venv` at it is **not** an option: `.venv` is a
*tracked* symlink whose committed value is `../bitcoin-education.old-20260709/
.venv`. Committing a change to it would repoint the production checkout on the
next pull. The symlink was therefore left exactly as committed.

`httpx` was missing in the development environment and is required by the
OpenAI client (`from openai import OpenAI` in `services/claude_service.py`); it
was installed into that isolated environment only.

Verified afterwards:

- imports resolve to this worktree from any working directory, without
  `PYTHONPATH`, when the development interpreter is used by absolute path;
- the full suite runs there: **4244 passed**;
- the production checkout still resolves `btcedu` to production code, and
  `.venv` is unmodified in git.

No systemd unit references the worktree's `.venv`; production services use
absolute paths under `/home/pi/AI-Startup-Lab/bitcoin-education/.venv`, and the
preview unit already used the dedicated development environment.

## Resolved — "two drafts rejected" vs. "no article was ever drafted"

Both statements are true; they describe different layers. The durable record is
the database, not the manifest.

| Layer | Evidence | Count |
| --- | --- | --- |
| Model reply | `llm` operations keyed `draft_article:…` | 2, completed |
| Semantic check | `llm` operations keyed `check_article_consistency:…` | 2, completed |
| Deterministic validation | `article` operations left in flight | 2 |
| Persisted revision | `news_article_revisions` | **0** |

`workflow.py` passes `checked_draft` as the drafter, so each attempt spends two
model calls before `generate_article_revision` validates anything. In
`article.py` a rejection raised *inside* the drafter — that is, by the semantic
check — sets `RECONCILE_REQUIRED` with the message
`Semantic article check rejected the draft`. Neither operation carries that
state. The drafts therefore passed the semantic check and were rejected
afterwards by the deterministic `_validate_draft_against_claims`, which before
fix 5 had no handler at all and left the operation at `submitted` — exactly the
state that made every retry fail with `requires reconciliation before retry`.

So: the model did produce two Turkish drafts, and no article revision was ever
stored. There is no contradiction.

**Limitation, stated plainly:** the concrete rejection reason for those two
drafts is not recoverable. The pre-fix code path never wrote `error_message`,
the ledger stores only cost and token counts, and the run manifest was later
overwritten by the offline bound-verification run. The current `error_message`
values on both operations are wording from my own offline reconciliation, not
run evidence. With fix 5 in place, a future rejection records its reason.

Before the live test the evidence database was copied to
`data/almanya24-preview/current-news.sqlite.pre-livetest.bak`.


## Bounded one-story live test — result

Approved scope: story `bundesweiter-warntag` only, `openai/gpt-4o`, at most 12
provider calls, at most 0.25 USD including retries.

The run was executed in several resumed passes because each pass exposed one
further defect. Everything already paid for was reused from the operation
ledger, so no answer was bought twice.

### Consumption against the approval

| Bound | Approved | Used |
| --- | --- | --- |
| Provider calls | 12 | **11** (10 recorded + 1 billed but discarded, see fix 7) |
| Cost | 0.25 USD | **0.078317 USD** recorded, plus the one discarded call |
| Stories | 1 | 1 |
| Model | `openai/gpt-4o` | `openai/gpt-4o` |

The ledger also holds three `fake`/0.001 USD entries from the offline guard
verification; they are excluded above. The run stopped on the call cap, not on
the cost cap: one call remained and a further repair needs two.

### Stages actually reached

| Stage | Outcome |
| --- | --- |
| Source retrieval | real, free, 3 documents |
| Claim extraction | real gpt-4o call |
| Evidence linking | real, 5 supporting passages accepted |
| Claim assessment | `supported` for the story's claims |
| Media selection | 1 cleared item |
| Article drafting | **real Turkish drafts produced** |
| Semantic consistency check | passed |
| Deterministic article validation | **refused both drafts** |
| Persisted article revision | none |
| Public site output | nothing — refusal is correct |

### Defects found and fixed during the run

6. **Attribution had to sit inside the cited sentence.** German reported speech
   attributes once and continues in Konjunktiv I ("Aus Sicht von
   Innenministerin Magdalena Finke (CDU) … In Schleswig-Holstein gebe es
   aktuell rund 2.800 Sirenen."). `_attributing_context()` now also searches the
   two sentences preceding the passage *in the same document*, and accepts the
   family name on second reference only when the full name is present in that
   document. An attribution further away is still refused.
7. **A reply without the `{"result": …}` envelope was thrown away.** In JSON
   mode gpt-4o answers the schema directly. `KeyError: 'result'` discarded a
   paid article draft and left the operation `reconcile_required`.
   `_unwrapped()` accepts both shapes; the schema is still validated afterwards.
   This is the one billed call whose cost was never recorded.
8. **A `failed` operation blocked its own retry.** `BudgetedCaller` only
   accepted `completed` or `reserved`, so a decided failure raised "Uncertain
   model operation requires reconciliation" forever. A failure is now
   re-reserved; only `reconcile_required` still blocks.
9. **Numbers carried solely by a claim statement counted as invented.** The
   extractor leaves `numeric_value` empty for "In Schleswig-Holstein gibt es
   aktuell rund 2.800 Sirenen", so an article repeating 2.800 was refused.
   `_checked_numbers()` also reads the figures out of the claim statements,
   which went through evidence linking with the rest of the sentence.
10. **A refused draft could never be repaired.** The identical payload returns
    the identical draft from the ledger. `generate_article_revision` now makes
    exactly one bounded repair attempt that hands the deterministic verdict back
    to the model as `rejected_reason`. No gate was changed.

### Where it actually stands

Two real Turkish drafts were produced and both were refused by checks that were
right to refuse them:

- draft 3 dropped the attribution of an attributed figure — the paragraph gave
  the 23-million-euro budget without naming Magdalena Finke;
- draft 4, after the repair, introduced a quotation for which no quoted claim
  exists.

Neither is a false positive. **No article revision was stored, so nothing is
publishable, and that is the correct outcome rather than a failure of the
gates.** Getting past it needs another repair round, which exceeds the approved
call budget by one call.

### Protected review view

Because there is no published article, the evidence a human needs was rendered
into the access-protected preview at `/_inceleme/`
(`data/almanya24-preview/build_review_page.py`). It shows the four drafts held
in the ledger, the deterministic refusals, the claims with their verdicts and
the real supporting passages with source links. The page carries `noindex`,
states plainly that nothing is approved or published, and no article was marked
published anywhere.

Verified: `200` on the local preview, `401` for an anonymous external request,
stylesheet resolves, five external source links, viewport meta present. Headless
Chromium screenshots could not be produced on this Pi (the zygote process fails
repeatedly), so the visual check is limited to structure and served resources.

### Correction to the limitation stated above

The two drafts of the *first* failed run are not lost after all: `draft_article`
is a `completed` `llm` operation and its reply is stored in `usage_json`. Only
the deterministic *reason* for their refusal was never written, because the
pre-fix code path stored no `error_message`. The review page therefore shows all
four drafts.
