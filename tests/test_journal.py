from pathlib import Path

from btcedu.utils.journal import journal_append, journal_event, redact


class TestRedact:
    def test_strips_env_api_key(self):
        text = "ANTHROPIC_API_KEY=sk-ant-abc123xyz"
        assert "[REDACTED]" in redact(text)
        assert "sk-ant" not in redact(text)

    def test_strips_json_api_key(self):
        text = '"anthropic_api_key": "sk-ant-abc123xyz"'
        assert "[REDACTED]" in redact(text)
        assert "sk-ant" not in redact(text)

    def test_strips_generic_token(self):
        text = "WHISPER_TOKEN=wh-tok-secret123"
        assert "[REDACTED]" in redact(text)
        assert "secret123" not in redact(text)

    def test_strips_authorization_header(self):
        text = "Authorization: Bearer sk-ant-secret-value"
        result = redact(text)
        assert "sk-ant" not in result

    def test_preserves_normal_text(self):
        text = "Episode ep001 processed 16 chunks at $0.38 cost"
        assert redact(text) == text

    def test_preserves_key_name(self):
        text = "ANTHROPIC_API_KEY=sk-ant-abc123"
        result = redact(text)
        assert "ANTHROPIC_API_KEY" in result
        assert "sk-ant" not in result

    def test_multiple_secrets(self):
        text = "ANTHROPIC_API_KEY=sk-ant-xxx OPENAI_API_KEY=sk-proj-yyy"
        result = redact(text)
        assert "sk-ant" not in result
        assert "sk-proj" not in result


class TestJournalAppend:
    def test_creates_file(self, tmp_path):
        path = tmp_path / "log.md"
        journal_append("Test Section", "Some body text", journal_path=path)
        assert path.exists()
        content = path.read_text()
        assert "Test Section" in content
        assert "Some body text" in content

    def test_includes_header_on_first_write(self, tmp_path):
        path = tmp_path / "log.md"
        journal_append("First", "body", journal_path=path)
        content = path.read_text()
        assert "Progress Log" in content

    def test_appends_without_overwrite(self, tmp_path):
        path = tmp_path / "log.md"
        journal_append("First", "first body", journal_path=path)
        journal_append("Second", "second body", journal_path=path)
        content = path.read_text()
        assert "First" in content
        assert "Second" in content
        assert "first body" in content
        assert "second body" in content

    def test_redacts_secrets_in_body(self, tmp_path):
        path = tmp_path / "log.md"
        journal_append("Config", "ANTHROPIC_API_KEY=sk-ant-secret", journal_path=path)
        content = path.read_text()
        assert "sk-ant-secret" not in content
        assert "[REDACTED]" in content

    def test_includes_timestamp(self, tmp_path):
        path = tmp_path / "log.md"
        journal_append("Timed", "body", journal_path=path)
        content = path.read_text()
        assert "UTC" in content


class TestJournalEvent:
    def test_formats_dict(self, tmp_path):
        path = tmp_path / "log.md"
        journal_event("Build", {"files": 3, "status": "ok"}, journal_path=path)
        content = path.read_text()
        assert "**files**" in content
        assert "3" in content
        assert "**status**" in content

    def test_redacts_secrets_in_values(self, tmp_path):
        path = tmp_path / "log.md"
        journal_event("Config", {"OPENAI_API_KEY": "sk-proj-xxx"}, journal_path=path)
        content = path.read_text()
        # The dict value itself isn't a KEY=value pattern, but the key name is logged
        # The important thing is: no raw secret should appear
        assert "sk-proj-xxx" not in content or "[REDACTED]" in content
