---
name: gemini_frame_edit
model: gemini-2.0-flash-exp
temperature: 0.2
description: Edit extracted video frames — translate German text overlays to Turkish
author: system
---

Edit this video frame from a German news broadcast (tagesschau) for a Turkish-language version.

## Instructions

1. **Identify all German text** visible in the frame: chyrons (lower thirds), headlines, tickers, statistics, labels, captions, location names, date/time displays.

2. **Translate all German text to Turkish.** Use formal news register (e.g., "Almanya" not "Almanya'da", proper Turkish news style). Keep numbers, proper nouns, and abbreviations as-is unless they have a standard Turkish equivalent.

3. **Preserve the visual style exactly**: same font style, color, size, position, background boxes, gradients, and transparency. The result must look like it was originally produced in Turkish.

4. **Do NOT alter**: photographs, maps, logos (tagesschau, ARD), background graphics, video content, people, or any non-text visual elements.

5. **If there is no German text visible** in the frame, return the image unchanged.

## Context

- Chapter: {{ chapter_title }}
- Chapter narration (Turkish): {{ narration_text }}
{% if visual_description %}- Visual description: {{ visual_description }}{% endif %}

## Quality Requirements

- Output must be the same resolution as the input
- Text must be sharp and readable
- No artifacts, blurring, or color shifts outside the text areas
- Professional broadcast quality
