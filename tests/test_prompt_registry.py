"""Tests for PromptRegistry."""

import tempfile
from pathlib import Path

import pytest

from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry
from btcedu.models.prompt_version import PromptVersion


SAMPLE_TEMPLATE = """\
---
name: test_prompt
model: claude-sonnet-4-20250514
temperature: 0.2
max_tokens: 8192
description: A test prompt
author: tester
---

You are a helpful assistant.

Do the thing.
"""

SAMPLE_TEMPLATE_V2 = """\
---
name: test_prompt
model: claude-sonnet-4-20250514
temperature: 0.3
max_tokens: 8192
description: A test prompt v2
author: tester
---

You are a helpful assistant.

Do the thing differently.
"""

SAMPLE_TEMPLATE_SAME_BODY_DIFF_META = """\
---
name: test_prompt
model: claude-opus-4-20250514
temperature: 0.9
max_tokens: 16384
description: Same body different metadata
author: someone_else
---

You are a helpful assistant.

Do the thing.
"""

NO_FRONTMATTER = """\
Just plain text content.

No frontmatter here.
"""


@pytest.fixture
def tmp_templates(tmp_path):
    """Create a temporary templates directory with a sample template."""
    tmpl = tmp_path / "test_prompt.md"
    tmpl.write_text(SAMPLE_TEMPLATE, encoding="utf-8")
    return tmp_path


@pytest.fixture
def registry(db_session, tmp_templates):
    return PromptRegistry(db_session, templates_dir=tmp_templates)


class TestPromptRegistry:
    def test_compute_hash_deterministic(self, registry):
        h1 = registry.compute_hash(SAMPLE_TEMPLATE)
        h2 = registry.compute_hash(SAMPLE_TEMPLATE)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_compute_hash_strips_frontmatter(self, registry):
        h1 = registry.compute_hash(SAMPLE_TEMPLATE)
        h2 = registry.compute_hash(SAMPLE_TEMPLATE_SAME_BODY_DIFF_META)
        assert h1 == h2  # Same body, different metadata -> same hash

    def test_compute_hash_different_content(self, registry):
        h1 = registry.compute_hash(SAMPLE_TEMPLATE)
        h2 = registry.compute_hash(SAMPLE_TEMPLATE_V2)
        assert h1 != h2

    def test_load_template_parses_frontmatter(self, registry, tmp_templates):
        tmpl_path = tmp_templates / "test_prompt.md"
        metadata, body = registry.load_template(tmpl_path)

        assert metadata["name"] == "test_prompt"
        assert metadata["model"] == "claude-sonnet-4-20250514"
        assert metadata["temperature"] == 0.2
        assert metadata["max_tokens"] == 8192
        assert metadata["description"] == "A test prompt"

    def test_load_template_returns_body(self, registry, tmp_templates):
        tmpl_path = tmp_templates / "test_prompt.md"
        metadata, body = registry.load_template(tmpl_path)

        assert "You are a helpful assistant." in body
        assert "Do the thing." in body
        assert "---" not in body  # frontmatter stripped from body

    def test_load_template_no_frontmatter(self, registry, tmp_path):
        tmpl_path = tmp_path / "plain.md"
        tmpl_path.write_text(NO_FRONTMATTER, encoding="utf-8")

        metadata, body = registry.load_template(tmpl_path)
        assert metadata == {}
        assert "Just plain text content." in body

    def test_register_version_creates_record(self, registry, tmp_templates, db_session):
        tmpl_path = tmp_templates / "test_prompt.md"
        pv = registry.register_version("test_prompt", tmpl_path)

        assert pv.name == "test_prompt"
        assert pv.version == 1
        assert pv.model == "claude-sonnet-4-20250514"
        assert pv.temperature == 0.2
        assert pv.max_tokens == 8192
        assert len(pv.content_hash) == 64

        # Verify in DB
        result = db_session.query(PromptVersion).filter(PromptVersion.name == "test_prompt").all()
        assert len(result) == 1

    def test_register_version_auto_increments(self, registry, tmp_templates, tmp_path):
        tmpl_path = tmp_templates / "test_prompt.md"
        pv1 = registry.register_version("test_prompt", tmpl_path)
        assert pv1.version == 1

        # Write v2 template with different body
        tmpl_v2 = tmp_path / "test_prompt_v2.md"
        tmpl_v2.write_text(SAMPLE_TEMPLATE_V2, encoding="utf-8")

        pv2 = registry.register_version("test_prompt", tmpl_v2)
        assert pv2.version == 2

    def test_register_version_deduplicates_by_hash(self, registry, tmp_templates):
        tmpl_path = tmp_templates / "test_prompt.md"
        pv1 = registry.register_version("test_prompt", tmpl_path)
        pv2 = registry.register_version("test_prompt", tmpl_path)

        assert pv1.id == pv2.id  # Same record returned
        assert pv1.version == pv2.version

    def test_register_version_dedup_with_different_metadata(self, registry, tmp_path):
        """Same body but different frontmatter metadata should deduplicate."""
        tmpl1 = tmp_path / "v1.md"
        tmpl1.write_text(SAMPLE_TEMPLATE, encoding="utf-8")
        pv1 = registry.register_version("test_prompt", tmpl1)

        tmpl2 = tmp_path / "v2.md"
        tmpl2.write_text(SAMPLE_TEMPLATE_SAME_BODY_DIFF_META, encoding="utf-8")
        pv2 = registry.register_version("test_prompt", tmpl2)

        assert pv1.id == pv2.id  # Same body hash -> same version

    def test_get_default_returns_default(self, registry, tmp_templates):
        tmpl_path = tmp_templates / "test_prompt.md"
        pv = registry.register_version("test_prompt", tmpl_path, set_default=True)

        result = registry.get_default("test_prompt")
        assert result is not None
        assert result.id == pv.id
        assert result.is_default is True

    def test_get_default_returns_none(self, registry):
        result = registry.get_default("nonexistent")
        assert result is None

    def test_get_default_returns_none_when_no_default_set(self, registry, tmp_templates):
        tmpl_path = tmp_templates / "test_prompt.md"
        registry.register_version("test_prompt", tmpl_path)  # not set_default

        result = registry.get_default("test_prompt")
        assert result is None

    def test_promote_to_default(self, registry, tmp_templates, tmp_path):
        tmpl_path = tmp_templates / "test_prompt.md"
        pv1 = registry.register_version("test_prompt", tmpl_path, set_default=True)

        tmpl_v2 = tmp_path / "v2.md"
        tmpl_v2.write_text(SAMPLE_TEMPLATE_V2, encoding="utf-8")
        pv2 = registry.register_version("test_prompt", tmpl_v2)

        # pv1 is default, pv2 is not
        assert registry.get_default("test_prompt").id == pv1.id

        # Promote pv2
        registry.promote_to_default(pv2.id)

        default = registry.get_default("test_prompt")
        assert default.id == pv2.id
        assert default.is_default is True

        # pv1 is no longer default
        from sqlalchemy.orm import Session
        db = registry._session
        db.refresh(pv1)
        assert pv1.is_default is False

    def test_promote_to_default_raises_on_missing(self, registry):
        with pytest.raises(ValueError, match="not found"):
            registry.promote_to_default(9999)

    def test_get_history_ordered(self, registry, tmp_templates, tmp_path):
        tmpl_path = tmp_templates / "test_prompt.md"
        registry.register_version("test_prompt", tmpl_path)

        tmpl_v2 = tmp_path / "v2.md"
        tmpl_v2.write_text(SAMPLE_TEMPLATE_V2, encoding="utf-8")
        registry.register_version("test_prompt", tmpl_v2)

        history = registry.get_history("test_prompt")
        assert len(history) == 2
        assert history[0].version == 2  # descending order
        assert history[1].version == 1

    def test_get_history_empty(self, registry):
        history = registry.get_history("nonexistent")
        assert history == []

    def test_register_with_set_default(self, registry, tmp_templates):
        tmpl_path = tmp_templates / "test_prompt.md"
        pv = registry.register_version("test_prompt", tmpl_path, set_default=True)

        assert pv.is_default is True
        assert registry.get_default("test_prompt").id == pv.id

    def test_register_with_explicit_params(self, registry, tmp_templates):
        """Explicit params override frontmatter metadata."""
        tmpl_path = tmp_templates / "test_prompt.md"
        pv = registry.register_version(
            "test_prompt",
            tmpl_path,
            model="claude-opus-4-20250514",
            temperature=0.9,
            max_tokens=16384,
        )

        assert pv.model == "claude-opus-4-20250514"
        assert pv.temperature == 0.9
        assert pv.max_tokens == 16384

    def test_load_system_template(self, db_session):
        """Can load the actual system.md template file."""
        registry = PromptRegistry(db_session)  # uses default TEMPLATES_DIR
        system_path = TEMPLATES_DIR / "system.md"

        assert system_path.exists(), f"system.md not found at {system_path}"

        metadata, body = registry.load_template(system_path)
        assert metadata["name"] == "system"
        assert metadata["model"] == "claude-sonnet-4-20250514"
        assert "ZORUNLU KURALLAR" in body
        assert "Bitcoin" in body

    def test_register_system_template(self, db_session):
        """Can register the actual system.md template."""
        registry = PromptRegistry(db_session)
        system_path = TEMPLATES_DIR / "system.md"

        pv = registry.register_version("system", system_path, set_default=True)
        assert pv.name == "system"
        assert pv.version == 1
        assert pv.is_default is True
        assert pv.model == "claude-sonnet-4-20250514"
        assert len(pv.content_hash) == 64

    def test_strip_frontmatter_static(self):
        result = PromptRegistry._strip_frontmatter(SAMPLE_TEMPLATE)
        assert "---" not in result
        assert "You are a helpful assistant." in result

    def test_strip_frontmatter_no_frontmatter(self):
        result = PromptRegistry._strip_frontmatter(NO_FRONTMATTER)
        assert "Just plain text content." in result
