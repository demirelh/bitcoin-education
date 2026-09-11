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
    scripts/almanya24_preview/build_current_preview.py \
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
/home/pi/.venvs/almanya24-newsroom-dev/bin/python scripts/almanya24_preview/build_current_preview.py
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
(`scripts/almanya24_preview/build_review_page.py`). It shows the four drafts held
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

## Raised call limit: the story reached a stored article revision

The operator raised the call ceiling for `bundesweiter-warntag` from 12 to 20
provider calls, leaving the 0.25 USD total unchanged and explicitly including
the paid-but-discarded call. The remaining work was a targeted repair of the
existing draft, not a fresh research run.

### Cost accounting, completed

The ledger of this approval holds **12 recorded calls at 0.090 USD**. One
further call was paid for and then discarded by the missing-envelope defect
(fix 7); its cost was never recorded and cannot be reconstructed, so a
conservative **0.015 USD** is reserved for it — above the most expensive call
observed in the whole run (0.013997 USD). Total charged against the approval:
**13 calls, at most 0.105 USD of the approved 0.25 USD.**

Two of the entries in the harness ledger are `fake` rows worth 0.003 USD from
the offline threshold test and are not provider calls. Because the harness
resumes its counter from the manifest, the first attempt at the raised ceiling
was blocked immediately by its own guard at zero cost; the limits then had to be
expressed in the harness's own cumulative terms (`--max-calls 17`,
`--budget-usd 0.238`), which is what keeps the real total at or below 0.25 USD.

### The quotation gate was wrong, not the draft

The refusal that ended the previous round — "Paragraph presents a quotation
without a quoted claim to back it" — was a **false positive**. `_QUOTED`
treated the straight apostrophe as a quotation mark, but Turkish separates
suffixes from proper nouns with exactly that character. A single paragraph
containing `Schleswig-Holstein'de` and `Euro'dan` therefore looked like one
single-quoted passage. The earlier report called this refusal legitimate; that
was wrong.

`_quotable()` now drops apostrophes that stand between two letters before the
scan. A real quotation mark never does, so genuine single quotes remain
detectable, and `’` was added to the delimiter class, which closes a hole rather
than opening one.

### A German role noun cannot be demanded inside Turkish prose

With the quotation error gone, the next refusal was
`Paragraph drops the attribution 'Sprecherin'`. The check required the
attribution verbatim. For a personal name that is right — `Magdalena Finke`
travels into every language unchanged — but `Sprecherin` does not, so the rule
silently forbade every claim a source attributed to an unnamed official.

`_is_personal_name()` now separates the two cases. A named source must still
appear verbatim; an unnamed role must be attributed by an explicit marker in the
article's own language (`ATTRIBUTION_MARKERS`). Dropping the attribution
entirely is still refused.

### The repair step was under-informed

It received one violation at a time and no evidence, so each defect cost a paid
round and the model could reintroduce the previous one. `_draft_violations()`
now reports every violation, `supporting_passages()` supplies the checked
passages with publisher and URL, and the repair payload carries the accumulated
list. An unchanged repair payload is refused before the call, because an
identical payload only buys the same draft back from the ledger.

### A local budget stop is not an uncertain provider outcome

The run then failed with `Uncertain model operation requires reconciliation`.
The harness's own call guard had raised a plain `RuntimeError` *before sending
anything*, and `BudgetedCaller` recorded that as `reconcile_required`, which
permanently blocked the retry. Nothing was sent and nothing was billed.
`ProviderCallNotAttempted` (in `core/editorial/jobs.py`) now marks that case;
the reservation is returned to `reserved` and the story simply resumes. A real
transport failure still requires reconciliation, because the request may have
arrived.

### Result

The story now produces a **stored, technically validated Turkish article
revision** — the first in this project. Consumption for the repair itself was
two provider calls; every subsequent step ran from the ledger at zero cost.

Two further defects surfaced after that and were fixed:

* the preview harness looked up the topic's research run with `.one()`, which
  fails once a topic has been researched more than once;
* `inspect_image()` rejected the selected Commons photograph because Pillow
  reports a Multi-Picture Object as `image/mpo` while Commons declares
  `image/jpeg`. MPO is a JPEG stream with more than one frame — same magic
  bytes, same decoder — so this discarded ordinary, correctly licensed press
  photographs. `_SAME_FORMAT` now accepts it; a PNG declared as JPEG is still
  refused.

With the picture attached, the media hash changed and a **second** article
revision was created, which is the intended invalidation behaviour.

### What was not done

The article is stored with status `draft`. It was **not** approved: the harness
gained a `--no-approve` mode so the real draft is not marked approved on the
operator's behalf, and the public site correctly shows nothing. The draft,
its picture with CC0 credit and licence, the claims with verdicts and the source
passages are rendered in the access-protected review view at `/_inceleme/`.

Verified: `200` locally, `401` anonymously from outside, the article absent from
the public start page. Screenshots remain impossible on this Pi.

The role `İçişleri Bakanı` in the Turkish text was checked against the source:
NDR writes `Innenministerin Magdalena Finke (CDU)`, so it is supported.

### Full-suite verification and one unrelated defect it exposed

The full suite was run twice, sequentially and in file order
(`pytest -q -p no:randomly`, 1 h 05 min each on this Pi). Both runs reported
`1 failed, 4254 passed`, always the same test:
`tests/test_web_avatar.py::TestApprovingFromTheDashboard::test_a_complete_episode_can_be_approved`.

The failure is unrelated to the editorial work — that test imports nothing from
`btcedu/core/editorial/` — but it is deterministic, so it was tracked down
rather than dismissed. Reduced reproduction:
`pytest tests/test_avatar_concurrency.py tests/test_web_avatar.py -q -p no:randomly`
(one minute). It disappears under `gc.disable()` and under any instrumentation
of the code path, which is what pointed at the mechanism.

`_get_session()` in `btcedu/web/api.py` hands out sessions that are never
closed. In the test harness the engine is an in-memory SQLite with a
`StaticPool`, so **every** session in the process shares one DBAPI connection.
When the garbage collector reclaims one of the leaked sessions it resets that
shared connection, and the `ROLLBACK` lands between the breaker's `INSERT` and
its `COMMIT`. `avatar_breaker.get_or_create()` then returned an instance whose
row no longer existed, and the first attribute read inside `_refresh_state()`
raised `ObjectDeletedError` — a `500` on a dashboard page that only wanted to
display whether submissions are allowed.

Production uses a file database with one connection per session, so the
interleaving does not occur there. The exposed weakness is real all the same:
the breaker row belongs to no single session, and a row can legitimately
disappear underneath a held instance. `get_or_create()` now reads the state back
before handing the row out and re-establishes it if it has gone, bounded to
three attempts; failing to establish it raises rather than inventing a breaker
view. Two regression tests cover both branches.

Nothing about the breaker's policy changed: the failure classes that count, the
thresholds, the cooldowns and the operator-signed reset are untouched.
