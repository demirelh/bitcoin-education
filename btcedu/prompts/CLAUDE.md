# btcedu/prompts/ — Prompt Templates & Builders

## v2 Templates (btcedu/prompts/templates/)

Format: YAML frontmatter + Jinja2 body.

```yaml
---
name: template_name
version: 1
model: claude-sonnet-4-20250514
temperature: 0.3
max_tokens: 4096
---
Jinja2 template body with {{ variables }}
```

Templates: `correct_transcript.md`, `translate.md`, `adapt.md`, `chapterize.md`, `imagegen.md`, `system.md`, `intent_extract.md`, `stock_rank.md`, `gemini_frame_edit.md`, `segment_broadcast.md`, `gemini_frame_edit.md`, `segment_broadcast.md`

`PromptRegistry` in `core/prompt_registry.py` loads templates, computes SHA-256 hash, registers `PromptVersion` in DB. Use `btcedu prompt list` to view registered versions.

## v1 Legacy Builders (Python modules)

`system.py`, `outline.py`, `script.py`, `shorts.py`, `qa.py`, `visuals.py`, `publishing.py`, `refine_script.py`, `refine_outline.py`

These build prompts programmatically for v1 pipeline. Do not modify unless fixing v1 bugs.

## Conventions

- Template variables come from the calling core module (e.g., `{{ transcript }}`, `{{ reviewer_feedback }}`)
- `correct_transcript.md` supports `{{ reviewer_feedback }}` injection for re-correction after review
- Most templates target `claude-sonnet-4-20250514`. Exception: `gemini_frame_edit.md` targets `gemini-2.0-flash-exp`
