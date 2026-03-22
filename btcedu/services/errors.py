"""Error classification for the btcedu pipeline.

Classifies exceptions into transient (retryable) vs permanent categories,
and provides user-friendly error messages with actionable suggestions.
"""

import logging
import re
from enum import Enum

logger = logging.getLogger(__name__)


class ErrorCategory(str, Enum):
    """Classification of pipeline errors."""

    TRANSIENT_RATE_LIMIT = "rate_limit"
    TRANSIENT_NETWORK = "network"
    TRANSIENT_SERVER = "server_error"
    PERMANENT_AUTH = "auth_error"
    PERMANENT_CONTENT = "content_error"
    PERMANENT_NOT_FOUND = "not_found"
    PERMANENT_COST_LIMIT = "cost_limit"
    UNKNOWN = "unknown"


# Patterns for classifying errors by exception message
_RATE_LIMIT_PATTERNS = re.compile(
    r"rate.?limit|429|quota.?exceeded|too.?many.?requests", re.IGNORECASE
)
_NETWORK_PATTERNS = re.compile(
    r"connect(ion)?.*?(error|refused|reset|timeout)|timed?\s*out|"
    r"name.?resolution|dns|unreachable|broken.?pipe",
    re.IGNORECASE,
)
_SERVER_ERROR_PATTERNS = re.compile(
    r"\b50[0-4]\b|internal.?server.?error|bad.?gateway|"
    r"service.?unavailable|gateway.?timeout",
    re.IGNORECASE,
)
_AUTH_PATTERNS = re.compile(
    r"\b40[13]\b|unauthorized|forbidden|invalid.*(api.?)?key|"
    r"authentication|permission.?denied|access.?denied",
    re.IGNORECASE,
)
_CONTENT_PATTERNS = re.compile(
    r"content.?policy|safety.?system|moderation|"
    r"validation.*error|invalid.*prompt|rejected",
    re.IGNORECASE,
)
_NOT_FOUND_PATTERNS = re.compile(
    r"\b404\b|not.?found|no.?such.?file|does.?not.?exist|missing", re.IGNORECASE
)
_COST_PATTERNS = re.compile(
    r"cost.?limit|budget.?exceeded|max.*cost", re.IGNORECASE
)

# Map well-known exception types to categories
_EXCEPTION_TYPE_MAP: dict[str, ErrorCategory] = {
    "ConnectionError": ErrorCategory.TRANSIENT_NETWORK,
    "ConnectTimeout": ErrorCategory.TRANSIENT_NETWORK,
    "ReadTimeout": ErrorCategory.TRANSIENT_NETWORK,
    "Timeout": ErrorCategory.TRANSIENT_NETWORK,
    "TimeoutError": ErrorCategory.TRANSIENT_NETWORK,
    "ConnectionResetError": ErrorCategory.TRANSIENT_NETWORK,
    "BrokenPipeError": ErrorCategory.TRANSIENT_NETWORK,
    "RateLimitError": ErrorCategory.TRANSIENT_RATE_LIMIT,
    "AuthenticationError": ErrorCategory.PERMANENT_AUTH,
    "PermissionError": ErrorCategory.PERMANENT_AUTH,
    "ValidationError": ErrorCategory.PERMANENT_CONTENT,
    "FileNotFoundError": ErrorCategory.PERMANENT_NOT_FOUND,
}

ERROR_SUGGESTIONS: dict[ErrorCategory, str] = {
    ErrorCategory.TRANSIENT_RATE_LIMIT: (
        "API rate limit reached. Pipeline will auto-retry. Check API quota."
    ),
    ErrorCategory.TRANSIENT_NETWORK: (
        "Network error. Check internet connection on Raspberry Pi."
    ),
    ErrorCategory.TRANSIENT_SERVER: (
        "External API server error. Usually resolves on its own."
    ),
    ErrorCategory.PERMANENT_AUTH: (
        "Invalid API key. Check .env for correct API keys."
    ),
    ErrorCategory.PERMANENT_CONTENT: (
        "Content rejected by API safety filter. Review/edit the prompt."
    ),
    ErrorCategory.PERMANENT_NOT_FOUND: (
        "Required resource not found. Check episode files on disk."
    ),
    ErrorCategory.PERMANENT_COST_LIMIT: (
        "Episode cost limit exceeded. Increase max_episode_cost_usd or skip."
    ),
    ErrorCategory.UNKNOWN: "Check logs for details.",
}

_TRANSIENT_CATEGORIES = frozenset({
    ErrorCategory.TRANSIENT_RATE_LIMIT,
    ErrorCategory.TRANSIENT_NETWORK,
    ErrorCategory.TRANSIENT_SERVER,
})


def classify_error(exc: Exception) -> ErrorCategory:
    """Classify an exception into a transient or permanent category.

    Uses exception type first, then falls back to message pattern matching.
    """
    # Check exception type name (works across SDK exception hierarchies)
    for parent in type(exc).__mro__:
        if parent.__name__ in _EXCEPTION_TYPE_MAP:
            return _EXCEPTION_TYPE_MAP[parent.__name__]

    # Fall back to message pattern matching
    msg = str(exc)

    if _COST_PATTERNS.search(msg):
        return ErrorCategory.PERMANENT_COST_LIMIT
    if _RATE_LIMIT_PATTERNS.search(msg):
        return ErrorCategory.TRANSIENT_RATE_LIMIT
    if _AUTH_PATTERNS.search(msg):
        return ErrorCategory.PERMANENT_AUTH
    if _CONTENT_PATTERNS.search(msg):
        return ErrorCategory.PERMANENT_CONTENT
    if _NOT_FOUND_PATTERNS.search(msg):
        return ErrorCategory.PERMANENT_NOT_FOUND
    if _SERVER_ERROR_PATTERNS.search(msg):
        return ErrorCategory.TRANSIENT_SERVER
    if _NETWORK_PATTERNS.search(msg):
        return ErrorCategory.TRANSIENT_NETWORK

    return ErrorCategory.UNKNOWN


def is_transient(category: ErrorCategory) -> bool:
    """Return True if the error category is transient (retryable)."""
    return category in _TRANSIENT_CATEGORIES


class PipelineError(Exception):
    """Enriched exception with error category and actionable suggestion."""

    def __init__(
        self,
        message: str,
        category: ErrorCategory,
        original: Exception | None = None,
        suggestion: str | None = None,
    ):
        self.category = category
        self.original = original
        self.suggestion = suggestion or ERROR_SUGGESTIONS.get(
            category, "Check logs for details."
        )
        full_msg = f"[{category.value}] {message} — {self.suggestion}"
        super().__init__(full_msg)
