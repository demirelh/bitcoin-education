# News Editorial Policy — tagesschau_tr Profile

**Last updated:** 2026-03-16
**Applies to:** All episodes with `content_profile=tagesschau_tr`

---

## Purpose

This document defines the editorial standards for processing ARD tagesschau broadcasts through the btcedu pipeline for Turkish-language output. Reviewers must apply these standards at every review gate.

---

## Source Rights

- **Source:** ARD tagesschau (publicly funded German broadcaster, ARD/Das Erste)
- **Availability:** Broadcasts are uploaded to the official tagesschau YouTube channel
- **Use:** Transformative Turkish-language derivative for educational purposes
- **Attribution:** Mandatory in every output (overlay, description, metadata)
- **Takedown:** If ARD requests removal, use `content_profile=tagesschau_tr` to bulk-identify and remove all affected episodes

**Do NOT:** redistribute German audio or verbatim German text in public output.

---

## Factual Accuracy Standards

1. **Every claim must trace to the source transcript.** If a Turkish sentence cannot be traced to a specific German sentence in the transcript, it must be removed or flagged.
2. **No invented facts.** If something was not said in the broadcast, it must not appear in the output.
3. **Numbers and statistics must be exact.** Check percentage points, vote counts, Eurozone figures — do not round or estimate.
4. **Names must be correct.** Politician names, party names (CDU, SPD, Grüne, AfD, FDP, Linke, BSW), institutional names must match the source.
5. **Dates must be exact.** Broadcast date in `source_attribution` must match the actual broadcast date.

---

## Political Neutrality Checklist

Review each story for:

- [ ] No editorial adjectives added to descriptions of political parties (e.g., do not add "extremist", "moderate", "controversial" unless the source says so)
- [ ] No characterization of political positions beyond what the source states
- [ ] Attribution language preserved ("laut Ministerium" → "bakanlığa göre", not "bakanlık açıkladı" if source used hedging)
- [ ] Uncertainty markers preserved ("soll", "angeblich" → "iddia edildiğine göre", "bildirilen")
- [ ] No opinion markers added ("ne yazık ki", "maalesef", "endişe verici")

---

## Attribution Requirements

Every tagesschau output MUST contain:

1. **stories.json `source_attribution` block** — written by the segment stage
2. **chapters.json first chapter overlay** — "Kaynak: ARD tagesschau — btcedu Türkçe"
3. **chapters.json last chapter overlay** — same attribution text
4. **YouTube description** — "Bu içerik ARD tagesschau yayınından Türkçe'ye çevrilmiştir. Orijinal kaynak: tagesschau.de"
5. **Mandatory disclaimer in narration/overlay** — "Bu içerik ARD tagesschau yayınından Türkçe'ye çevrilmiştir."

---

## Review Gate 1 (Post-Correction)

**Goal:** Verify the German transcript was correctly cleaned without altering meaning.

**Review checklist:**
- [ ] Political party names correctly spelled (CDU, SPD, Grüne, AfD, FDP, Linke, BSW)
- [ ] Institutional names correct (Bundestag, Bundesrat, Bundesverfassungsgericht, EU-Kommission)
- [ ] Politician names correctly spelled (check against original audio/video if needed)
- [ ] City/location names correct (Brüssel not Brüsel, Kiew, Moskau, Berlin)
- [ ] No content was removed or added
- [ ] Percentage/vote figures unchanged

**Reject if:**
- A correction changed a factual claim (even if the original ASR error seems obvious)
- An institutional name was silently changed
- Political party attribution was altered

---

## Translation Review Gate (review_gate_translate)

**Goal:** Verify the Turkish translation is factually accurate, politically neutral, formally registered, and properly attributed before it becomes video narration.

**When it appears:** After the TRANSLATED status in the tagesschau_tr pipeline, replacing the bitcoin_podcast adaptation review gate (review_gate_2).

**Review mode:** Bilingual — each story shown as a German/Turkish side-by-side pair.

**The 6-item editorial checklist:**

| # | Check | Description |
|---|-------|-------------|
| 1 | Factual accuracy verified | Every claim in Turkish traces to the German source |
| 2 | No editorialization or political spin | No added adjectives, no characterizations beyond source |
| 3 | Source attribution included | "haberlere göre", "Bakanlığın açıklamasına göre" preserved |
| 4 | Names, places, institutions correct | Bundestag (Almanya Federal Meclisi), politician names, cities |
| 5 | No invented facts or figures | Word counts comparable (DE vs TR; flagged if >50% difference) |
| 6 | Formal news register (not conversational) | Haber dili, not gunluk konusma; "siz" not "sen" |

**When to approve:**
- All 6 checklist items pass
- Word ratio warnings are explained (e.g., a story was intentionally brief in German)
- Institutional names correctly expanded with Turkish parenthetical on first occurrence

**When to request changes (targeted re-translation):**
- A specific institution name is wrong (e.g., Bundesrat → wrong Turkish expansion)
- Register slips in one or two stories
- Attribution language dropped from a specific story
- Minor proper noun error
- Notes must name the specific story IDs and issues

**When to reject (full re-translation):**
- Factual error (wrong number, wrong party attribution, invented statistic)
- Editorialization present (added "maalesef", "endişe verici", political adjectives)
- Multiple stories have hallucinated content
- A story in the German source is missing from the Turkish output
- Overall word count is <50% of German (systematic summarization detected)

**Per-story review workflow:**
1. For each story, click Accept / Reject / Edit in the review UI
2. For EDITED stories: provide corrected Turkish text directly
3. For REJECTED stories: the story is marked `[ÇEVİRİ REDDEDİLDİ]` in the sidecar
4. Click "Apply decisions" to write `stories_translated.reviewed.json`
5. Click "Approve" to advance the pipeline to chapterization

**Sidecar workflow:**
- When you click "Apply decisions", a sidecar file is written:
  `data/outputs/{episode_id}/review/stories_translated.reviewed.json`
- The chapterizer automatically uses this sidecar instead of the raw translation
- If you approve without applying per-story decisions, the unreviewed translation is used

**Reversion behavior:**
- Request changes → episode reverts to SEGMENTED, translator re-runs with your feedback
- Feedback is injected into the translate prompt via `{{ reviewer_feedback }}`
- Rejection → same as request changes (episode reverts to SEGMENTED)

---

## Review Gate 3 (Post-Render Video)

**Goal:** Verify the final video meets all editorial standards before publication.

**Review checklist:**
- [ ] Attribution overlay visible in intro (first ~5 seconds)
- [ ] Attribution overlay visible in outro (last chapter)
- [ ] No editorialization in narration text
- [ ] Story order matches broadcast order (check against original YouTube video)
- [ ] No stories omitted without justification
- [ ] Proper nouns correctly pronounced in TTS (check for politician name mispronunciation)
- [ ] Turkish text is in formal news register (haber dili), not colloquial
- [ ] Numbers and figures stated correctly in Turkish
- [ ] Mandatory disclaimer appears in text/overlay

**Reject if:**
- Attribution overlay missing from intro or outro
- Any story was silently dropped
- Clear factual error in narration (wrong number, wrong party attribution)
- Gross TTS pronunciation error on key names

**Request changes if:**
- Minor TTS pronunciation issues (non-critical names)
- Slightly informal phrasing that doesn't affect factual accuracy

---

## When to Escalate

Escalate to the content owner if:

1. **Source material is disputed** — if the tagesschau broadcast contained a retraction or correction after the broadcast date, flag for re-processing
2. **Takedown request** — if ARD contacts about content removal, immediately unpublish all tagesschau episodes and notify the content owner
3. **Factual error in source** — if the original broadcast itself contained a factual error that was later corrected by ARD, document this in the episode notes and do not silently fix it (preserve the source faithfully)
4. **Ambiguous translation** — if a key political term has no clear Turkish equivalent, escalate rather than guessing

---

## Non-Goals

This policy does NOT cover:

- Content filtering of news topics (we do not censor or omit stories)
- Weather forecast accuracy (TTS weather chapter is treated like any other)
- Video resolution or visual quality (handled by render policy)
- YouTube algorithm optimization (handled by publishing team)
