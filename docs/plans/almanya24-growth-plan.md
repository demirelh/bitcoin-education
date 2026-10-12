# ALMANYA24 – Plan für Reichweite und Zuschauerbindung

Stand: 2026-10-12. Grundlage: Bestandsaufnahme des Codes und Feedback eines
Kollegen („ohne Avatar langweilig; Aufzählungen und Wichtiges müssen im Bild
erscheinen“).

## Entscheidungen

| Frage | Entscheidung |
|---|---|
| Avatar | Stock-Avatar von HeyGen (keine echte Person, keine Einwilligung nötig) |
| Inhaltsquelle | Bei der ARD-Übersetzung bleiben, Risiko bewusst tragen |
| Veröffentlichung | Automatisch öffentlich, wenn alle Prüfungen grün sind |
| Zusatzformate | YouTube Shorts und Instagram Reels |
| Budget | über 300 USD pro Monat |

## Ausgangslage

- **Avatar:** Code zu ~95 % fertig (`core/anchor_*.py`, `scene_planner.py`,
  `studio_media.py`). `btcedu anchor-readiness` blockiert mit vier Befunden:
  kein Studio-Manifest, nur Platzhalter-Looks, keine Rechte-Datei, kein
  KI-Hinweistext. Kosten ca. 1 USD/Minute Avatar, bei 35–45 % Bildschirmzeit
  etwa 3–5 USD pro Sendung.
- **Bild:** Pro Kapitel ein einziges, unbewegtes Bild
  (`ken_burns_enabled: false`, wegen ruckelndem `zoompan`).
- **Einblendungen:** Bauchbinde, Themenkarte, Laufband, Wetterkarte, Untertitel.
  Das Schema kennt `quote` und `statistic`, der Kapitel-Prompt erzeugt aber nur
  eine Bauchbinde pro Kapitel. Keine Aufzählungen.
- **Zeitstempel pro Wort:** ElevenLabs liefert sie bereits und sie werden im
  TTS-Manifest gespeichert (`word_timings`).
- **Veröffentlichung:** Ziele `private`/`unlisted`, `auto_publish: false`.
  Vorschaubild ist das erste Kapitelbild; `core/thumbnail_generator.py` existiert,
  ist aber nur per CLI erreichbar.
- **Fehlt:** Shorts/Reels (nur Prompt-Skelett `prompts/shorts.py`),
  YouTube-Statistiken, Playlisten, Veröffentlichungszeitpunkt.

## Ziele (Kennzahlen)

| Kennzahl | Ziel |
|---|---|
| Zuschauer nach 30 Sekunden | ≥ 70 % |
| Klickrate Vorschaubild | ≥ 5 % |
| Durchschnittlich angesehener Anteil | ≥ 50 % |
| Video online | spätestens 21:30 |
| Shorts/Reels | jede Meldung zusätzlich als Clip |

## Phase 1 – Das Video lebendig machen (größter Hebel, ohne Zusatzkosten)

1. **Aufzählungen und Kernfakten auf dem Bildschirm**
   - Die Kapitelstufe markiert zusätzlich Aufzählungen, wichtige Zahlen und
     Zitate (neue Overlay-Typen, z. B. `bullet_list`, `number_highlight`).
   - Eine Prüfung stellt sicher, dass jeder eingeblendete Text wörtlich aus der
     freigegebenen Sprechfassung stammt; so wird nichts dazuerfunden.
   - Einblendung genau dann, wenn das Wort gesprochen wird (`word_timings`).
   - Darstellung als animierte Karte (HTML/SVG über Chromium wie die
     Wetterkarten) bzw. als einfache Texteinblendung für Zahlen.
2. **Bewegung statt Standbild**
   - Ken-Burns-Effekt reparieren: erst hochskalieren, dann zoomen.
   - Mehrere Bilder pro Kapitel (Person, Ort, Thema im Wechsel alle
     6–10 Sekunden) über die vorhandenen Bildsammlungen und Moderationsblöcke.
3. **Einstieg mit Sog:** In den ersten 10 Sekunden ein Überblick („Bugün: …“)
   mit den drei Top-Themen als animierte Liste, dazu eine Fortschrittsanzeige
   („2/7“).
4. **Karten für Ortsnennungen:** kleine Deutschland- oder Weltkarte mit
   markiertem Ort, deterministisch erzeugt wie die Wetterkarte.

## Phase 2 – Avatar (Stock-Avatar von HeyGen, ca. 3–5 USD pro Sendung)

5. **Code anpassen:** Bereitschaftsprüfung und Rechte-Datei unterscheiden
   Stock-Avatar und digitalen Zwilling. Für den Stock-Avatar entfällt die
   Personen-Einwilligung; der KI-Hinweistext bleibt Pflicht.
6. **Studio einmalig erstellen:** virtuelles Nachrichtenstudio im
   ALMANYA24-Design (Hintergrund, Pult, Bereich für Themenbilder). Entwurf durch
   die Entwicklung, Freigabe durch dich.
7. **Ablauf der Sendung:** Der Avatar spricht Einstieg, Überleitungen und
   Abschluss; bei Meldungen wechselt das Bild auf Themenbilder und Grafiken.
   Bildschirmanteil des Avatars rund 40 %.
8. **YouTube-Kennzeichnung:** Video als „synthetische Inhalte“ markieren,
   KI-Hinweis in die Beschreibung.

## Phase 3 – Shorts und Reels

9. **Pro Meldung ein Clip im Hochformat (9:16), 30–60 Sekunden:**
   Avatar-Ausschnitt oder Bild, große Untertitel, Aufzählungskarte und Hinweis
   auf das ganze Video. Die Teile aus Phase 1 und 2 werden wiederverwendet; es
   gibt keine zweite Render-Logik.
10. **Hochladen:** YouTube Shorts über die vorhandene YouTube-Anbindung,
    Instagram Reels über die Instagram Graph API (Business-Konto und Meta-App
    nötig).

## Phase 4 – Veröffentlichung und Wachstums-Kreislauf

11. **Automatisch öffentlich**, sobald alle Prüfungen grün sind. Bei Rot
    blockieren und per WhatsApp melden.
12. **Vorschaubilder:** `thumbnail_generator` in die Pipeline einbinden –
    Gesicht bzw. Foto aus der Bildsammlung plus 2–4 Wörter große türkische
    Schrift, mehrere Varianten.
13. **Titel nach Neugier-Muster** (wichtigstes Thema plus Zahl oder Frage),
    Playlisten nach Thema, angehefteter Kommentar, Link zur Website.
14. **YouTube-Statistiken ins Dashboard:** Aufrufe, Klickrate, Zuschauerkurve.
    Auswertung, welche Kapitel- und Grafikarten die Zuschauer halten.

## Phase 5 – Risiko und Betrieb

15. **ARD-Risiko (bewusst getragen), trotzdem kostenlos senken:**
    - nie ARD-Bild- oder Tonmaterial verwenden (heute schon so),
    - eigene Formulierung durch die Adaption,
    - Ablauf für Beschwerden oder Sperrungen: sofort zurückziehen und im
      Dashboard markieren.
16. **Kosten im Blick:** pro Monat etwa Avatar 100–150 USD, Bilder 20–40 USD,
    Sprachausgabe wie bisher; Budgetanzeige im Dashboard.

## Was nur du erledigen kannst

- **HeyGen:** API-Zugang, Stock-Avatar und Stimme auswählen (Entscheidung:
  Avatar-Stimme oder unsere ElevenLabs-Stimme).
- **Konten:** fal.ai und OpenAI aufladen.
- **Instagram:** Business-Konto, Facebook-Seite, Meta-App für die
  Upload-Schnittstelle.
- **YouTube:** Kanal-Banner, Logo, Kanalbeschreibung; Upload-Token verlängern.

## Reihenfolge

1. Phase 1 (Punkte 1–4) – braucht nichts von dir, größter Effekt.
2. Phase 4 (11–14) parallel zu Phase 2 (5–8).
3. Phase 3 (9–10).
