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

Templates: `correct_transcript.md`, `translate.md`, `adapt.md`, `chapterize.md`,
`imagegen.md`, `imagegen_news.md`, `qa_review.md`, `system.md`,
`intent_extract.md`, `stock_rank.md`, `gemini_frame_edit.md`,
`segment_broadcast.md`

Profile overrides live in `templates/<profile>/` (`bitcoin_podcast/`, `tagesschau_tr/`) and take
precedence over the same-named template in `templates/`. `tagesschau_tr/` additionally provides
`translate_intro_outro.md` and `script_broadcast.md`. Term glossaries:
`glossaries/glossary_bitcoin_tr.yaml`,
`glossaries/glossary_news_tr.yaml`.

`PromptRegistry` in `core/prompt_registry.py` loads templates, computes SHA-256 hash, registers `PromptVersion` in DB. Use `btcedu prompt list` to view registered versions.

## v1 Legacy Builders (Python modules)

`system.py`, `outline.py`, `script.py`, `shorts.py`, `qa.py`, `visuals.py`, `publishing.py`, `refine_script.py`, `refine_outline.py`

These build prompts programmatically for v1 pipeline. Do not modify unless fixing v1 bugs.

## Conventions

- Template variables come from the calling core module (e.g., `{{ transcript }}`, `{{ reviewer_feedback }}`)
- Conditional adaptation instructions must honor each story payload's `allowed_operations`; do not
  describe an operation as always active when the caller may omit it from that per-story list.
- `correct_transcript.md` supports `{{ reviewer_feedback }}` injection for re-correction after review
- Most templates target `claude-sonnet-4-20250514`. Exceptions: `gemini_frame_edit.md`
  (`gemini-2.0-flash-exp`), `qa_review.md` (`gpt-5.6-sol`),
  `tagesschau_tr/qa_review.md` and `tagesschau_tr/script_broadcast.md`
  (`gpt-5.6-sol`), `bitcoin_podcast/chapterize.md`
  (`claude-sonnet-4-5-20250929`)
- Automatic gate adjudication is configured in the profile YAML (provider `copilot_cli`, model `gpt-5.6-sol`), not in a prompt template

<!--
Documentation sync
Baseline: d1b4676
Synced through: current working tree
Date: 2026-08-22
-->
