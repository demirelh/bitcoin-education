"""Tests for error classification and retry decorator."""

from unittest.mock import patch

import pytest
import requests

from btcedu.services.errors import (
    ERROR_SUGGESTIONS,
    ErrorCategory,
    PipelineError,
    classify_error,
    is_transient,
)
from btcedu.services.retry import retry_on_transient

# ---------------------------------------------------------------------------
# Error Classification Tests
# ---------------------------------------------------------------------------


class TestClassifyError:
    """Test classify_error() with various exception types and messages."""

    def test_connection_error(self):
        exc = ConnectionError("Connection refused")
        assert classify_error(exc) == ErrorCategory.TRANSIENT_NETWORK

    def test_timeout_error(self):
        exc = TimeoutError("Read timed out")
        assert classify_error(exc) == ErrorCategory.TRANSIENT_NETWORK

    def test_requests_connection_error(self):
        exc = requests.ConnectionError("DNS resolution failed")
        assert classify_error(exc) == ErrorCategory.TRANSIENT_NETWORK

    def test_requests_timeout(self):
        exc = requests.Timeout("Request timed out")
        assert classify_error(exc) == ErrorCategory.TRANSIENT_NETWORK

    def test_rate_limit_message(self):
        exc = RuntimeError("HTTP 429: rate limit exceeded")
        assert classify_error(exc) == ErrorCategory.TRANSIENT_RATE_LIMIT

    def test_rate_limit_too_many(self):
        exc = RuntimeError("Too many requests, please slow down")
        assert classify_error(exc) == ErrorCategory.TRANSIENT_RATE_LIMIT

    def test_quota_exceeded(self):
        exc = RuntimeError("quota exceeded for this billing period")
        assert classify_error(exc) == ErrorCategory.TRANSIENT_RATE_LIMIT

    def test_server_500(self):
        exc = RuntimeError("Internal server error 500")
        assert classify_error(exc) == ErrorCategory.TRANSIENT_SERVER

    def test_server_502(self):
        exc = RuntimeError("502 Bad Gateway")
        assert classify_error(exc) == ErrorCategory.TRANSIENT_SERVER

    def test_server_503(self):
        exc = RuntimeError("503 Service Unavailable")
        assert classify_error(exc) == ErrorCategory.TRANSIENT_SERVER

    def test_auth_401(self):
        exc = RuntimeError("401 Unauthorized: Invalid API key")
        assert classify_error(exc) == ErrorCategory.PERMANENT_AUTH

    def test_auth_403(self):
        exc = RuntimeError("403 Forbidden: Access denied")
        assert classify_error(exc) == ErrorCategory.PERMANENT_AUTH

    def test_invalid_key(self):
        exc = RuntimeError("Invalid API key provided")
        assert classify_error(exc) == ErrorCategory.PERMANENT_AUTH

    def test_content_policy(self):
        exc = RuntimeError("content_policy_violation: unsafe content")
        assert classify_error(exc) == ErrorCategory.PERMANENT_CONTENT

    def test_safety_system(self):
        exc = RuntimeError("Blocked by safety system")
        assert classify_error(exc) == ErrorCategory.PERMANENT_CONTENT

    def test_not_found_404(self):
        exc = RuntimeError("HTTP 404: resource not found")
        assert classify_error(exc) == ErrorCategory.PERMANENT_NOT_FOUND

    def test_file_not_found(self):
        exc = FileNotFoundError("/data/audio/ep001/audio.m4a")
        assert classify_error(exc) == ErrorCategory.PERMANENT_NOT_FOUND

    def test_cost_limit(self):
        exc = RuntimeError("Episode cost limit exceeded: $12.50 > $10.00")
        assert classify_error(exc) == ErrorCategory.PERMANENT_COST_LIMIT

    def test_unknown_error(self):
        exc = RuntimeError("Something completely unexpected happened")
        assert classify_error(exc) == ErrorCategory.UNKNOWN

    def test_permission_error(self):
        exc = PermissionError("Operation not permitted")
        assert classify_error(exc) == ErrorCategory.PERMANENT_AUTH


class TestIsTransient:
    """Test is_transient() helper."""

    def test_rate_limit_is_transient(self):
        assert is_transient(ErrorCategory.TRANSIENT_RATE_LIMIT) is True

    def test_network_is_transient(self):
        assert is_transient(ErrorCategory.TRANSIENT_NETWORK) is True

    def test_server_is_transient(self):
        assert is_transient(ErrorCategory.TRANSIENT_SERVER) is True

    def test_auth_not_transient(self):
        assert is_transient(ErrorCategory.PERMANENT_AUTH) is False

    def test_content_not_transient(self):
        assert is_transient(ErrorCategory.PERMANENT_CONTENT) is False

    def test_cost_not_transient(self):
        assert is_transient(ErrorCategory.PERMANENT_COST_LIMIT) is False

    def test_unknown_not_transient(self):
        assert is_transient(ErrorCategory.UNKNOWN) is False


class TestPipelineError:
    """Test PipelineError enriched exception."""

    def test_basic_creation(self):
        exc = PipelineError(
            "API failed",
            ErrorCategory.TRANSIENT_NETWORK,
            original=ConnectionError("refused"),
        )
        assert exc.category == ErrorCategory.TRANSIENT_NETWORK
        assert "[network]" in str(exc)
        assert "API failed" in str(exc)
        assert exc.suggestion is not None

    def test_custom_suggestion(self):
        exc = PipelineError(
            "Custom error",
            ErrorCategory.UNKNOWN,
            suggestion="Do something specific.",
        )
        assert exc.suggestion == "Do something specific."
        assert "Do something specific." in str(exc)

    def test_default_suggestion(self):
        exc = PipelineError("Auth failed", ErrorCategory.PERMANENT_AUTH)
        assert exc.suggestion == ERROR_SUGGESTIONS[ErrorCategory.PERMANENT_AUTH]


# ---------------------------------------------------------------------------
# Retry Decorator Tests
# ---------------------------------------------------------------------------


class TestRetryOnTransient:
    """Test retry_on_transient() decorator."""

    def test_success_no_retry(self):
        """Successful call should not trigger any retry."""
        call_count = 0

        @retry_on_transient(max_retries=3)
        def succeed():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = succeed()
        assert result == "ok"
        assert call_count == 1

    @patch("btcedu.services.retry.time.sleep")
    def test_retry_on_transient_then_succeed(self, mock_sleep):
        """Should retry on transient error and eventually succeed."""
        call_count = 0

        @retry_on_transient(max_retries=3, base_delay=1.0, jitter=False)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Connection refused")
            return "recovered"

        result = flaky()
        assert result == "recovered"
        assert call_count == 3
        assert mock_sleep.call_count == 2  # Two retries before success

    @patch("btcedu.services.retry.time.sleep")
    def test_fail_fast_on_permanent(self, mock_sleep):
        """Should not retry on permanent errors."""
        call_count = 0

        @retry_on_transient(max_retries=3)
        def auth_fail():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("401 Unauthorized: Invalid API key")

        with pytest.raises(PipelineError) as exc_info:
            auth_fail()
        assert call_count == 1  # No retries
        assert mock_sleep.call_count == 0
        assert exc_info.value.category == ErrorCategory.PERMANENT_AUTH

    @patch("btcedu.services.retry.time.sleep")
    def test_exhaust_retries(self, mock_sleep):
        """Should raise PipelineError after exhausting retries."""

        @retry_on_transient(max_retries=2, base_delay=1.0, jitter=False)
        def always_fail():
            raise ConnectionError("Connection refused")

        with pytest.raises(PipelineError) as exc_info:
            always_fail()
        assert exc_info.value.category == ErrorCategory.TRANSIENT_NETWORK
        assert "3 attempts" in str(exc_info.value)
        assert mock_sleep.call_count == 2  # Two delays before final failure

    @patch("btcedu.services.retry.time.sleep")
    def test_exponential_backoff(self, mock_sleep):
        """Should use exponential backoff between retries."""

        @retry_on_transient(max_retries=3, base_delay=1.0, jitter=False)
        def always_fail():
            raise ConnectionError("timeout")

        with pytest.raises(PipelineError):
            always_fail()

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [1.0, 2.0, 4.0]  # 1 * 2^0, 1 * 2^1, 1 * 2^2

    @patch("btcedu.services.retry.time.sleep")
    def test_max_delay_cap(self, mock_sleep):
        """Delay should be capped at max_delay."""

        @retry_on_transient(max_retries=5, base_delay=10.0, max_delay=25.0, jitter=False)
        def always_fail():
            raise ConnectionError("timeout")

        with pytest.raises(PipelineError):
            always_fail()

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        # 10, 20, 25 (capped), 25 (capped), 25 (capped)
        assert all(d <= 25.0 for d in delays)
        assert delays[0] == 10.0  # base_delay * 2^0
        assert delays[1] == 20.0  # base_delay * 2^1
        assert delays[2] == 25.0  # capped

    @patch("btcedu.services.retry.time.sleep")
    def test_jitter_adds_randomness(self, mock_sleep):
        """Jitter should add 0-25% extra delay."""

        @retry_on_transient(max_retries=1, base_delay=1.0, jitter=True)
        def always_fail():
            raise ConnectionError("timeout")

        with pytest.raises(PipelineError):
            always_fail()

        actual_delay = mock_sleep.call_args_list[0].args[0]
        # Base delay is 1.0, jitter adds 0-25%, so range is [1.0, 1.25]
        assert 1.0 <= actual_delay <= 1.25

    @patch("btcedu.services.retry.time.sleep")
    def test_content_policy_no_retry(self, mock_sleep):
        """Content policy violations should not be retried."""

        @retry_on_transient(max_retries=3)
        def content_fail():
            raise RuntimeError("content_policy_violation: unsafe image")

        with pytest.raises(PipelineError) as exc_info:
            content_fail()
        assert exc_info.value.category == ErrorCategory.PERMANENT_CONTENT
        assert mock_sleep.call_count == 0

    @patch("btcedu.services.retry.time.sleep")
    def test_cost_limit_no_retry(self, mock_sleep):
        """Cost limit errors should not be retried."""

        @retry_on_transient(max_retries=3)
        def cost_fail():
            raise RuntimeError("Episode cost limit exceeded: $12 > $10")

        with pytest.raises(PipelineError) as exc_info:
            cost_fail()
        assert exc_info.value.category == ErrorCategory.PERMANENT_COST_LIMIT
        assert mock_sleep.call_count == 0

    def test_preserves_function_metadata(self):
        """Decorator should preserve function name and docstring."""

        @retry_on_transient()
        def my_function():
            """My docstring."""
            pass

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."
