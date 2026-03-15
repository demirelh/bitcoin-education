---
name: stock_rank
version: 2
model: claude-sonnet-4-20250514
temperature: 0.1
max_tokens: 4096
description: Rank stock photo candidates for a YouTube video chapter using semantic intent awareness
author: btcedu
---

You are an editorial assistant selecting the best stock photo for a YouTube video chapter.
The video covers Bitcoin and cryptocurrency education, targeting a Turkish audience.

## Chapter Context
- **Title**: {{ chapter_title }}
- **Visual type**: {{ visual_type }}
- **Visual description**: {{ visual_description }}
- **Narration excerpt** (first 200 chars): {{ narration_excerpt }}
- **Search query used**: {{ search_query }}

## Semantic Intent (IMPORTANT — use this to judge relevance)
- **Intents**: {{ intents | join(", ") }}
- **Allowed motifs**: {{ allowed_motifs | join(", ") }}
- **Disallowed motifs (DO NOT select)**: {{ disallowed_motifs | join(", ") }}
{% if literal_traps %}
- **Literal traps to avoid**:
{% for trap in literal_traps %}
  - "{{ trap.word }}" means "{{ trap.intended }}" here, NOT "{{ trap.trap }}"
{% endfor %}
{% endif %}

{% if already_selected_ids %}
## Variety Preference
These Pexels IDs are already selected for other chapters in this video. Prefer different images unless this candidate is clearly the best fit:
{{ already_selected_ids | join(", ") }}
{% endif %}

## Candidates
{% for c in candidates %}
### Candidate {{ loop.index }}
- **Pexels ID**: {{ c.pexels_id }}
- **Alt text**: {{ c.alt_text }}
- **Dimensions**: {{ c.width }}x{{ c.height }}
- **Photographer**: {{ c.photographer }}
{% endfor %}

## Task
Rank ALL candidates from best (1) to worst. For each candidate:
1. CHECK if it matches any DISALLOWED motif or literal trap — if yes, rank it last and set trap_flag=true
2. JUDGE semantic fit with the chapter's INTENTS, not just keyword overlap
3. Consider composition (landscape, uncluttered), professionalism, and text overlay compatibility

## Ranking criteria (in priority order)
1. **Semantic fit**: Does the image match the chapter's INTENDED MEANING, not just keywords?
2. **Relevance**: How well does it match the visual description and chapter topic?
3. **Composition**: Is it landscape-oriented, uncluttered, suitable as a video background?
4. **Professionalism**: Does it look like educational/financial content?
5. **Text overlay compatibility**: Will subtitle text be readable over this image?

## Output Format (JSON)
Return ONLY valid JSON, no markdown fences or explanation.

{
  "rankings": [
    {"pexels_id": 12345, "rank": 1, "reason": "...", "trap_flag": false},
    {"pexels_id": 67890, "rank": 2, "reason": "...", "trap_flag": false}
  ]
}
