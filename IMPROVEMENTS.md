# BTCEDU — Konkrete Verbesserungsvorschläge

Stand: 2026-07-10 nach Pipeline-Test mit 3 Bitcoin-Podcast-Episoden
(-GKkHVtrrpU, b_K-QlnmL1A, hQvvsPqc1FY)

## Was war kaputt (gefunden im Test-Lauf)

| # | Bug | Wirkung | Fix-Status |
|---|-----|--------|------------|
| 1 | `LLM_PROVIDER=openai` fest, obwohl Prompt-Frontmatter Claude Sonnet 4 anfordert | Claude Sonnet wurde nie benutzt. Alles über GPT-4o. | 🟡 Anthropic-Guthaben leer → OpenAI-Fallback aktiv |
| 2 | `claude_max_tokens=4096` Default, während Übersetzungen 8-10k Tokens brauchen | LLM wurde gezwungen zu komprimieren → 30% Zeichen-Ratio DE→TR (statt 85-95%) | ✅ Default → 16384. Prompt-frontmatter max_tokens jetzt durchgereicht. |
| 3 | `SEGMENT_CHAR_LIMIT=15_000` zu groß für 4k-Output-Fenster | Jeder Segment-Übersetzung zwang zur Kompression | ✅ Halbiert auf 6_000 |
| 4 | Adapter-Stage nutzte immer Bitcoin-Adapt-Prompt, nicht profil-namespaced | Tagesschau bekam Krypto-T1/T2-Regeln aufgezwungen | ✅ `resolve_template_path(profile=namespace)` |
| 5 | `[T1: ...]`/`[T2: ...]` Marker gingen ins TTS-Input | Sprecher hätte "T1 kaldırıldı" laut gesprochen | ✅ `strip_tier_markers()` vor Persist |
| 6 | Chapter-Config im YAML (`hook_max_seconds`, `min_chapter_seconds`) wurde nie gelesen | Bitcoin-Podcast-Hook-Zwang lief nicht | ✅ Template-Substitution im Chapterizer |
| 7 | Renderer nutzte nur globales `settings.render_*`, nicht `stage_config.render` | Beide Profile bekamen gleiche Feature-Flags | ✅ Per-Profil-Overrides mit ENV-Fallback |

## Ergebnis Textreue (Char-Ratio DE→TR)

| Episode | Vorher | Nachher | Delta |
|---------|-------:|--------:|------:|
| b_K-QlnmL1A (Faust/Goethe) | 32 % | **76 %** | +138 % |
| -GKkHVtrrpU / hQvvsPqc1FY | 26-28 % | (noch nicht neu ausgeführt) | — |

Optimal wäre 85-95 %. 76 % ist okay-nutzbar aber immer noch GPT-4o-Kompressions-Bias.

---

## Konkrete Verbesserungen — priorisiert

### 🥇 Priorität 1 — sofort, größte Wirkung

**P1.1 Claude Sonnet nutzen statt GPT-4o**
- Weg A (empfohlen): Anthropic-Guthaben laden ($20 reicht für ~20 Episoden), dann `LLM_PROVIDER=anthropic`. Sonnet 4.5 folgt Instruktions-Regeln ("nicht zusammenfassen!") viel zuverlässiger als GPT-4o.
- Weg B (kostenlos, wenn möglich): Über **GitHub Models Marketplace** (https://models.inference.ai.azure.com) mit dem GitHub-Token routen. Claude Sonnet ist dort verfügbar und für persönliche Nutzung frei. → Neuer LLM-Provider `github_models` in `claude_service.py` bauen.
- Weg C: **GPT-4-Turbo** statt gpt-4o (besser bei Long-Form-Faithfulness). Ein-Zeilen-Änderung im `.env`: `OPENAI_LLM_MODEL=gpt-4-turbo`.

**P1.2 Alle 3 Test-Episoden neu laufen lassen** mit den Fixes, dann Ratio erneut messen.

**P1.3 Model-Info in Frontmatter respektieren**  
Aktuell wird `settings.claude_model` benutzt und die Frontmatter-`model:` Angabe in den Prompt-Templates ignoriert.  
→ Fix in `claude_service.py`: bei jedem Aufruf das Template-`model` per Kwarg überschreiben können.

---

### 🥈 Priorität 2 — Content-Qualität

**P2.1 Zwei-Pass-Übersetzung**  
1. Pass 1: Sonnet 4.5 macht wörtliche, cümle-cümle Übersetzung mit hoher Fidelity.  
2. Pass 2: Ein leichter „Podcast-Ton-Refine" ändert nur Bindewörter/Fluss, nicht den Inhalt.  
→ Verhindert Kompression, gibt guten Sunucu-Flow.

**P2.2 Sentence-Alignment-Guard**  
Nach jeder Übersetzung: DE-Satz-Count vs. TR-Satz-Count vergleichen. Wenn Delta > 20 %, retry mit expliziter Nachfrage „Es fehlen ca. X Sätze — welche?".

**P2.3 Direkt-Zitate/Zahlen-Extraktion**  
Vor Übersetzung: alle wörtlichen Zitate (in Anführungszeichen) + Zahlen aus DE-Transkript extrahieren.  
Nach Übersetzung prüfen ob sie im TR vorkommen. Fehlt eines → retry.  
Speziell wichtig für Bitcoin-Podcasts (21 Millionen, 4-Jahres-Halving usw.) und Tagesschau (Datumsangaben, Prozente).

**P2.4 Domain-Glossar auch in Correct-Stage**  
Aktuell wird das Glossar nur in `translate` injiziert. Auch der Correct-Prompt sollte wissen, dass "Bitcoin" nicht zu "big coin" korrigiert wird, "Blocktrainer" ein Eigenname ist, etc.

**P2.5 Faktenkonsistenz-Check zwischen Kapiteln**  
Nach Chapterize: Ist eine Zahl in Kapitel 3 mit einer in Kapitel 7 konsistent? Häufiger Fehler bei GPT-Modellen ist Zahlenverwechslung nach Segment-Grenzen.

---

### 🥉 Priorität 3 — Podcast-Flow & Chapterize

**P3.1 Chapterize-JSON-Schema härten**  
Aktuell scheitert die Pydantic-Validierung bei 2 von 3 Läufen wegen fehlender `start_offset_seconds`/`duration_seconds` in Overlays.  
→ Im Prompt: konkrete JSON-Beispiele mit ALLEN Pflichtfeldern.  
→ In `_fix_chapter_data`: fehlende Overlay-Timings automatisch ergänzen (start=1s, dur=5s) statt Retry.

**P3.2 WPM-Kalibrierung**  
Aktuell hardcoded 150 WPM für Türkisch. Real gemessene ElevenLabs-POLAT-Stimme: ~130 WPM.  
→ Konfigurierbar in Profil-YAML pro Stimme. Sonst wird Dauer falsch geschätzt → Chapter zu kurz.

**P3.3 Hook aus 5 Kandidaten wählen**  
Aktuell nimmt der Prompt die ersten 3 Sätze als Hook. Besser: LLM sucht die 5 stärksten Kandidaten im Script und wählt den knackigsten.

**P3.4 Kapitel-Länge im Profil-YAML anpassen**  
Aktueller `bitcoin_podcast.yaml`: `min_chapter_seconds=30, max_chapter_seconds=90` → gibt 8-14 Chapters für 5-min-Video (zu granular).  
→ Für Podcast-Flow besser: `min=60, max=120` → 5-7 flüssige Kapitel mit Cliffhanger-Pausen.

**P3.5 Adapt-Stage prüft Char-Ratio**  
Nach Adapt: Adapted-Text sollte 85-105 % des Translation-Textes lang sein.  
Aktuell schrumpft Adapt weiter auf 40 % (max_tokens-Bug, jetzt gefixt aber untestet). Fidelity-Warning einbauen wie im Translator.

---

### 🎬 Priorität 4 — Tagesschau-spezifisch

**P4.1 Tagesschau-adapt.md testen**  
Der neue News-Adapt-Prompt (local_relevance-Tier, keine T1/T2, keine Krypto-Sprache) ist gebaut aber noch nicht mit einer echten Tagesschau-Episode getestet.  
→ Detect-Job manuell triggern und Ergebnis prüfen.

**P4.2 Gemini-Frame-Edit-Alternative**  
Gemini kostet pro Frame. Für 15-min-Sendung ~30 Frames = einige $.  
Alternative: **EasyOCR + PIL** (kostenlos, lokal).  
1. Chyron-Region erkennen (Standard-Tagesschau-Layout: unten-links, blau-weißer Balken)  
2. Text auslesen  
3. Übersetzen (im vorhandenen Translate-Prompt)  
4. Text-Region mit Blur/Solid überdecken  
5. TR-Text mit Roboto-Condensed-Bold darüber malen  
→ Wesentlich billiger, mehr Kontrolle, keine Halluzinationen.

**P4.3 Tagesschau-Voice manuell auswählen**  
`voice_id: ""` im Profil ist leer → Fallback auf Default-Voice (Krypto-POLAT).  
→ ElevenLabs Voice Library nach TR-News-Voice durchsuchen (formaler Register, kein Podcast-Ton). Kandidaten: Meltem, Emre.

**P4.4 Ticker mit echten News-Highlights**  
Aktueller Ticker-Text: `"|||"`-getrennte Chapter-Titel.  
Besser: Kurze News-Zusammenfassungen von den anderen Beiträgen aus derselben Sendung (Cross-Referenz aus Segmenter).

---

### 🔧 Priorität 5 — Systemisch

**P5.1 Fidelity-Warning ins Web-Dashboard**  
Wenn Ratio < 80 %, Episode als "review needed" markieren mit rotem Badge.

**P5.2 Cost/Fidelity-Historie pro Episode**  
Neue DB-Tabelle `quality_metrics(episode_id, stage, char_ratio, cost_usd, model, timestamp)`.  
Über Zeit Regressionen sehen (z.B. wenn ein Prompt-Update die Fidelity verschlechtert).

**P5.3 Auto-Approve-Modus**  
Für Bulk-Tests: `btcedu run --auto-approve` überspringt Review-Gates. Nur für Dev/Staging.

**P5.4 A/B-Prompt-Runs**  
`btcedu translate --episode-id X --prompt-variant B` läuft parallel mit einer alternativen Prompt-Version. Ergebnisse werden für Vergleich persistiert.

**P5.5 Anthropic-Guthaben-Check im Credits-Dashboard**  
Wenn LLM_PROVIDER=anthropic und Guthaben 0: rot markieren + Auto-Fallback zu openai mit visuellem Hinweis „using fallback".

**P5.6 GPU/Whisper-lokal**  
Aktuell Whisper Cloud-API (~$0.006/min → $0.09 pro 15-min-Episode).  
Bei mehr als ~30 Episoden/Monat lohnt sich `faster-whisper` lokal mit large-v3-turbo. Setup: `pip install faster-whisper`, ~2h Coding.

---

## Empfohlene Reihenfolge

1. **P1.1** Sonnet 4.5 einbinden (Anthropic laden oder GitHub Models)
2. **P1.2** Test-Episoden neu laufen lassen
3. **P2.1** Zwei-Pass-Übersetzung (biggest quality win)
4. **P3.1** Chapterize-JSON-Härtung + **P3.4** Kapitel-Länge korrigieren
5. **P4.1** Tagesschau mit echter Episode testen
6. **P2.2 + P2.3** Alignment-Guards (falls Fidelity weiter unter 85 %)
7. **P5.1 + P5.2** Fidelity im Dashboard + Historie

## Blockiert / Wartend

- **Anthropic-Guthaben laden** (aktueller Blocker für P1.1 Weg A)
- **Music-Assets** (Wave 1, 2 CC0-MP3s für Bitcoin/News-Bed)
- **Tagesschau-Voice-Pick** aus ElevenLabs Library
