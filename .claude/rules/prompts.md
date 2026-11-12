---
paths:
  - btcedu/prompts/**
---

# Prompt Template Rules

- Format: YAML frontmatter + Jinja2 body
- PromptRegistry loads templates, computes SHA-256 hash, registers PromptVersion in DB
- Frontmatter `model` is only recorded in `PromptVersion`; the model actually called is profile-owned (`btcedu/profiles/*.yaml`, passed to `call_claude()` as provider/model override)
- Template variables come from the calling core module (e.g., `{{ transcript }}`, `{{ reviewer_feedback }}`)
- v1 legacy Python builders (system.py, outline.py, etc.) must not be modified unless fixing v1 bugs
- Profile-specific templates go in subdirectories (e.g., `templates/tagesschau_tr/`)
