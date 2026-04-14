---
paths:
  - btcedu/prompts/**
---

# Prompt Template Rules

- Format: YAML frontmatter + Jinja2 body
- PromptRegistry loads templates, computes SHA-256 hash, registers PromptVersion in DB
- Most templates target `claude-sonnet-4-20250514`; exception: `gemini_frame_edit.md` targets `gemini-2.0-flash-exp`
- Template variables come from the calling core module (e.g., `{{ transcript }}`, `{{ reviewer_feedback }}`)
- v1 legacy Python builders (system.py, outline.py, etc.) must not be modified unless fixing v1 bugs
- Profile-specific templates go in subdirectories (e.g., `templates/tagesschau_tr/`)
