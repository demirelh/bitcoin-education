"""Credentials must not reach a log line, an error message or a traceback.

A key on disk is a key that has to be rotated. These tests pin down the two
ways one used to get there: a settings object printed as a whole, and a
library quoting a rejected value back in its own error text.
"""

from __future__ import annotations

import io
import logging

import pytest

from btcedu.config import Settings
from btcedu.utils.secrets import (
    SecretRedactingFilter,
    install_log_redaction,
    is_secret_field,
    redact_values,
    secret_values,
)

FAKE_ANTHROPIC = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF"
FAKE_ELEVEN = "el-abcdef0123456789abcdef0123456789"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key=FAKE_ANTHROPIC,
        elevenlabs_api_key=FAKE_ELEVEN,
        outputs_dir="data/outputs",
    )


@pytest.fixture
def captured(settings):
    """A logger wired exactly like the real one, with its output in hand."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("btcedu.test.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    install_log_redaction(settings, logger)
    return logger, stream


# ---------------------------------------------------------------------------
# the object itself
# ---------------------------------------------------------------------------


class TestSettingsDoesNotSpellOutItsKeys:
    def test_repr_masks_credentials(self, settings):
        assert FAKE_ANTHROPIC not in repr(settings)
        assert FAKE_ELEVEN not in repr(settings)
        assert "[REDACTED]" in repr(settings)

    def test_str_masks_credentials(self, settings):
        assert FAKE_ANTHROPIC not in str(settings)

    def test_interpolation_masks_credentials(self, settings):
        """An f-string in a log call is the most likely way this happens."""
        assert FAKE_ANTHROPIC not in f"Konfiguration: {settings}"

    def test_an_exception_carrying_settings_does_not_leak(self, settings):
        error = RuntimeError(f"bad config: {settings!r}")
        assert FAKE_ANTHROPIC not in str(error)

    def test_ordinary_values_stay_readable(self, settings):
        """Masking everything would cost diagnosis and buy nothing."""
        assert "data/outputs" in repr(settings)

    def test_an_unset_key_is_not_masked_into_looking_set(self):
        """Showing [REDACTED] for an empty key would hide a misconfiguration."""
        text = repr(Settings(anthropic_api_key=""))
        assert "anthropic_api_key=''" in text


# ---------------------------------------------------------------------------
# which fields count
# ---------------------------------------------------------------------------


class TestWhatCountsAsACredential:
    @pytest.mark.parametrize(
        "name",
        [
            "anthropic_api_key",
            "elevenlabs_api_key",
            "github_token",
            "notify_whatsapp_token",
            "client_secret",
            "db_password",
            "aws_credential",
        ],
    )
    def test_credentials_are_recognised(self, name):
        assert is_secret_field(name)

    @pytest.mark.parametrize(
        "name",
        [
            "youtube_client_secrets_path",
            "youtube_credentials_path",
            "notify_whatsapp_url",
            "outputs_dir",
            "render_fps",
        ],
    )
    def test_locations_and_settings_are_not_credentials(self, name):
        """A path to a credentials file is a location. Blanking it out of the
        logs would cost diagnosis and hide nothing."""
        assert not is_secret_field(name)

    def test_only_real_values_are_collected(self, settings):
        values = secret_values(settings)
        assert FAKE_ANTHROPIC in values
        assert "data/outputs" not in values

    def test_empty_keys_are_not_collected(self):
        """An empty string would match everywhere and blank out whole logs."""
        assert "" not in secret_values(Settings(anthropic_api_key=""))

    def test_a_short_value_is_not_struck_from_free_text(self):
        """A four-character key would hit unrelated words in every message."""
        assert "abc" not in secret_values(Settings(anthropic_api_key="abc"))

    def test_an_object_that_is_not_settings_yields_nothing(self):
        from unittest.mock import MagicMock

        assert secret_values(MagicMock()) == set()
        assert secret_values(object()) == set()


# ---------------------------------------------------------------------------
# the leak that actually happened
# ---------------------------------------------------------------------------


class TestTheLeakThatHappened:
    def test_a_library_quoting_a_rejected_key_is_redacted(self, captured):
        """The real one: an invalid header value came back inside the error
        text of the HTTP library and was logged verbatim."""
        logger, stream = captured

        logger.warning("ElevenLabs request error: Header part (%s) is invalid", FAKE_ELEVEN)

        assert FAKE_ELEVEN not in stream.getvalue()
        assert "[REDACTED]" in stream.getvalue()

    def test_the_surrounding_message_survives(self, captured):
        """Redaction that swallows the diagnosis is its own kind of failure."""
        logger, stream = captured

        logger.warning("ElevenLabs request error: Header part (%s) is invalid", FAKE_ELEVEN)

        assert "ElevenLabs request error" in stream.getvalue()
        assert "is invalid" in stream.getvalue()

    def test_a_whole_settings_object_in_a_log_line_is_redacted(self, captured, settings):
        logger, stream = captured

        logger.error("Konfiguration: %s", settings)

        assert FAKE_ANTHROPIC not in stream.getvalue()

    def test_a_key_inside_a_traceback_is_redacted(self, captured):
        logger, stream = captured

        try:
            raise RuntimeError(f"auth failed for {FAKE_ANTHROPIC}")
        except RuntimeError:
            logger.exception("Aufruf fehlgeschlagen")

        output = stream.getvalue()
        assert FAKE_ANTHROPIC not in output
        assert "Aufruf fehlgeschlagen" in output

    def test_a_key_in_the_format_string_itself_is_redacted(self, captured):
        logger, stream = captured

        logger.info(f"token={FAKE_ELEVEN}")

        assert FAKE_ELEVEN not in stream.getvalue()

    def test_a_key_split_across_several_arguments(self, captured):
        logger, stream = captured

        logger.info("a=%s b=%s", FAKE_ANTHROPIC, FAKE_ELEVEN)

        output = stream.getvalue()
        assert FAKE_ANTHROPIC not in output
        assert FAKE_ELEVEN not in output


# ---------------------------------------------------------------------------
# the replacement itself
# ---------------------------------------------------------------------------


class TestRedactValues:
    def test_untouched_when_nothing_matches(self):
        assert redact_values("alles in Ordnung", {FAKE_ANTHROPIC}) == "alles in Ordnung"

    def test_every_occurrence_goes(self):
        text = f"{FAKE_ELEVEN} und nochmal {FAKE_ELEVEN}"
        assert FAKE_ELEVEN not in redact_values(text, {FAKE_ELEVEN})

    def test_a_key_containing_another_is_not_half_replaced(self):
        """Replacing the short one first would leave the long one's tail
        readable in the log."""
        short = "abcdefghij"
        long = "abcdefghijklmnop"

        result = redact_values(f"key={long}", {short, long})

        assert "klmnop" not in result

    def test_empty_text_is_fine(self):
        assert redact_values("", {FAKE_ANTHROPIC}) == ""


class TestInstallation:
    def test_repeated_installation_does_not_stack_filters(self, settings):
        logger = logging.getLogger("btcedu.test.stacking")
        logger.handlers = [logging.StreamHandler(io.StringIO())]

        install_log_redaction(settings, logger)
        install_log_redaction(settings, logger)

        filters = [f for f in logger.handlers[0].filters if isinstance(f, SecretRedactingFilter)]
        assert len(filters) == 1

    def test_a_configuration_without_keys_installs_nothing(self):
        logger = logging.getLogger("btcedu.test.nokeys")
        logger.handlers = [logging.StreamHandler(io.StringIO())]
        bare = Settings(
            anthropic_api_key="",
            openai_api_key="",
            whisper_api_key="",
            elevenlabs_api_key="",
            github_token="",
            fal_api_key="",
            ideogram_api_key="",
            gemini_api_key="",
            pexels_api_key="",
        )

        install_log_redaction(bare, logger)

        assert not [
            f for f in logger.handlers[0].filters if isinstance(f, SecretRedactingFilter)
        ]

    def test_a_record_still_passes_through(self, captured):
        """A filter returning False would silently drop log lines."""
        logger, stream = captured

        logger.info("eine ganz gewöhnliche Meldung")

        assert "eine ganz gewöhnliche Meldung" in stream.getvalue()
