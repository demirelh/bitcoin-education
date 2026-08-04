# Dual-presenter news format — self review

A critical read of the change after it had been run end to end. This is written
against the implementation, not against the plan: the plan is easy to satisfy on
paper, the question is what will actually break in production.

## What the review is based on

- The full diff of the feature and its follow-up fixes.
- One complete real run of episode `IuNt7iyNtkI` — script, chapterize, imagegen,
  TTS, render — with real providers.
- Offline re-runs of ranking and script assembly against four episodes.
- The full test suite (2012 passing).

## What held up

**The narration lock.** This was the highest risk in the design: a second writer
of narration text next to the approved translation. It held, and when it did not,
it failed loudly and correctly. The `_merge_short_chapters()` bug would have
shipped a video whose weather chapter contained the closing twice. The lock
caught it before a single cent was spent on TTS. Keeping `canonical_narration()`
untouched and putting the script preference only inside
`_enforce_narration_lock()` was the right call.

**Backward compatibility.** v1 episodes, profiles without `script.enabled`, and
profiles without configured voices all take the old path. This was verified by
tests rather than by inspection, which matters because the fallbacks are the part
nobody exercises manually.

**Cost discipline.** The deterministic fallback means a failed or disabled LLM
call does not stop the programme, and the script path made chapterize free
($0.86 → $0.00) because the mapping no longer needs a model.

## What was wrong and why it was missed

**The grounding heuristic was written against English intuitions.** Capitalisation
carries far less information in a Turkish all-caps headline than in English prose,
and agglutination breaks exact matching. The unit tests passed because they used
invented examples that happened to fit the heuristic. Only real text exposed it.
Lesson: a linguistic heuristic must be validated against real output of the target
language before it is given `major` severity.

**The speech rate was inherited, never measured.** 150 wpm was a plausible-looking
constant that had been in the code and was simply carried forward. It was 23 % too
fast, which made every duration estimate wrong and sent the ranking after a
problem that did not exist. Two rounds of tuning `delivery_factor` were spent
compensating for a miscalibrated constant. Lesson: before tuning a compensator,
verify the measurement it compensates for.

**The duration gate was asymmetric by accident, not by design.** Nobody decided
that a short programme is acceptable; the branch simply had not been written.

**In-place mutation with a discarded return value.** `_merge_short_chapters()`
looks pure at the call site. It is not. This is the kind of bug that only shows up
when a second caller appears, which is exactly what happened.

**The branding guard only saw text, not pictures.** The guard was built to stop
the source name appearing on screen, and it did that job for overlays and titles.
Meanwhile the profile's own image style prefix instructed the picture model to
imitate the source broadcaster *by name*, and the model duly printed that name
into the frame. The guard and the prompt contradicted each other and nothing
compared them. Only looking at a rendered frame exposed it. Lesson: a guard that
inspects metadata gives no protection against a generator that produces pixels
from a prompt the guard never reads.

## What remains weak

**`delivery_factor = 0.88` is an empirical constant fitted to one model on a
handful of episodes.** It is not a law. If the editorial model or the prompt
changes, it will drift, and the symptom will be programmes that are consistently
too long or too short. It should be re-measured whenever `script_broadcast.md` or
the model changes. A more honest design would measure the realised ratio per
episode and feed a rolling average back in — that was not built.

**Script QA is a mix of deterministic checks and heuristics with the same
severity vocabulary.** A deterministic duration violation and a heuristic name
suspicion both surface as `major`. An operator cannot tell from the severity alone
how much to trust the finding.

**The stale `script_qa.json` problem.** Artifacts are written at stage time and are
not recomputed when a shared constant changes. `IuNt7iyNtkI` still carries a
`duration_too_short` finding computed at 150 wpm while the live endpoint computes
9.3 minutes. It self-corrects on the next episode, but it means a stored QA
artifact is not necessarily consistent with current code. Regenerating it would
have cost an LLM call and invalidated $2.67 of TTS, so it was left — a defensible
trade-off, but it is a trade-off.

**One end-to-end observation.** The multi-voice path, the ranking and the new
overlays have each been seen working once at full scale. That is enough to say
they work; it is not enough to say they are stable across the variety of episodes
the pipeline will actually see.

**Rendering cost of failure is high.** A render is roughly an hour on the Pi. A
failure at chapter 5 wastes most of it. Segment reuse exists, but the directory
robustness fix was only added after a real failure — there may be similar
assumptions about long-lived state elsewhere in the render path.

## What was not built, and honestly why

- **Intro master asset.** The procedural intro is branded and works. A hand-made
  clip needs artwork the repository does not have. This is a decision for the
  operator, not something to invent.
- **Rolling delivery-factor feedback.** Described above; deliberately not built
  because a self-tuning constant is harder to reason about than a documented one.
- **Automatic publishing.** Off by requirement, unchanged.

## Concrete follow-ups worth doing

1. Re-measure the realised words-per-minute after the next two or three episodes
   and confirm 122 holds.
2. Re-measure `delivery_factor` whenever the script prompt or model changes.
3. Separate deterministic findings from heuristic ones in the QA severity model.
4. Extract and look at a frame from every episode. Two of the most serious faults
   found so far (the printed source name, the lower third layout) were invisible
   in every artifact and only visible in the picture.
5. Watch the anchor share across several episodes; it was 45 % on the observed
   episode against a 50 % target, which is inside tolerance but consistently low.
