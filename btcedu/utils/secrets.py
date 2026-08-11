"""Keep credentials out of logs and error messages.

Two different leaks, two different remedies.

A settings object printed anywhere — in a log line, in an exception, in a
traceback — carries every key the process holds. That one is closed at the
source by masking the representation itself, so no caller has to remember
anything.

The other leak comes from outside: a library that puts a rejected header or
a malformed value into its own error text. Nothing here can stop that text
from being written, so the configured values are struck out of log records
instead, wherever in the message they appear.
"""

from __future__ import annotations

import logging

# Names whose value is a credential. Matched as substrings of the field name.
_SECRET_MARKERS = ("api_key", "token", "secret", "password", "credential")

# A name may look like a secret and hold something harmless. A path to a
# credentials file is a location, not a credential, and blanking it out of
# the logs would cost diagnosis and buy nothing.
_NOT_SECRET_SUFFIXES = ("_path", "_dir", "_file", "_url")

# Below this length a value is too generic to strike out of arbitrary text;
# doing so would mangle unrelated messages.
_MIN_SECRET_LENGTH = 8

_PLACEHOLDER = "[REDACTED]"


def is_secret_field(name: str) -> bool:
    """Whether a settings field name denotes a credential."""
    lowered = name.lower()
    if lowered.endswith(_NOT_SECRET_SUFFIXES):
        return False
    return any(marker in lowered for marker in _SECRET_MARKERS)


def secret_values(settings) -> set[str]:
    """Every credential this configuration actually holds.

    Only strings long enough to be struck out of free text without hitting
    something else by accident.
    """
    values: set[str] = set()
    fields = getattr(type(settings), "model_fields", None)
    if not isinstance(fields, dict):
        return values
    for name in fields:
        if not is_secret_field(name):
            continue
        value = getattr(settings, name, None)
        if not isinstance(value, str):
            continue
        # A field may hold several credentials at once (a list of fallback
        # keys). Struck out only as one long string, each individual key
        # would still reach the log the moment it is used on its own.
        for part in [value, *value.split(",")]:
            candidate = part.strip()
            if len(candidate) >= _MIN_SECRET_LENGTH:
                values.add(candidate)
    return values


def redact_values(text: str, values) -> str:
    """Replace known credential values wherever they appear in *text*."""
    if not text:
        return text
    result = text
    # Longest first: a key that contains another must not be half-replaced.
    for value in sorted(values, key=len, reverse=True):
        if value and value in result:
            result = result.replace(value, _PLACEHOLDER)
    return result


class SecretRedactingFilter(logging.Filter):
    """Strike configured credentials out of every record that passes.

    Sits on the handler rather than on a single logger, because the leak that
    prompted this came from a third-party library's own error text, not from
    any call this project makes.
    """

    def __init__(self, values):
        super().__init__()
        self._values = {v for v in values if v}

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._values:
            return True

        # Formatting here rather than touching msg and args separately: a
        # credential can sit in either, or be split across both.
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken record is not ours to fix
            return True

        redacted = redact_values(message, self._values)
        if redacted != message:
            record.msg = redacted
            record.args = ()

        if record.exc_info:
            record.exc_text = redact_values(
                record.exc_text or logging.Formatter().formatException(record.exc_info),
                self._values,
            )
            record.exc_info = None

        return True


def install_log_redaction(settings, logger: logging.Logger | None = None) -> None:
    """Attach the filter to every handler of the root logger.

    Idempotent: repeated calls replace the existing filter rather than
    stacking, so a re-read configuration takes effect.
    """
    values = secret_values(settings)
    if not values:
        return

    target = logger or logging.getLogger()
    for handler in target.handlers:
        handler.filters = [f for f in handler.filters if not isinstance(f, SecretRedactingFilter)]
        handler.addFilter(SecretRedactingFilter(values))
