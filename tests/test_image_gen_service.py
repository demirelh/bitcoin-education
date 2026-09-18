"""gpt-image-1 retry behaviour for rate limits vs. an exhausted credit balance."""

from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from btcedu.services.image_gen_service import DallE3ImageService


def _rate_limit_error(body: dict, message: str) -> openai.RateLimitError:
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://api.openai.com/v1/images/generations"),
    )
    return openai.RateLimitError(message, response=response, body=body)


def _client_raising(error: Exception) -> MagicMock:
    client = MagicMock()
    client.images.generate.side_effect = error
    return client


def test_credit_exhaustion_is_not_retried():
    """OpenAI returns an empty balance as 429. Retrying cannot help and the
    message must name the real cause instead of a rate limit.
    """
    service = DallE3ImageService(api_key="test-key")
    error = _rate_limit_error(
        {"code": "credit_balance_exhausted"},
        "Error code: 429 - Your credit balance is exhausted",
    )
    client = _client_raising(error)

    with (
        patch("openai.OpenAI", return_value=client),
        patch("btcedu.services.image_gen_service.time.sleep") as mock_sleep,
        pytest.raises(RuntimeError, match="credit balance exhausted"),
    ):
        service._call_dalle3_with_retry("a prompt", "1024x1024", "standard")

    assert client.images.generate.call_count == 1
    mock_sleep.assert_not_called()


def test_real_rate_limit_is_still_retried():
    service = DallE3ImageService(api_key="test-key")
    error = _rate_limit_error({}, "Error code: 429 - Rate limit reached for images")
    client = _client_raising(error)

    with (
        patch("openai.OpenAI", return_value=client),
        patch("btcedu.services.image_gen_service.time.sleep"),
        pytest.raises(RuntimeError, match="rate limit exceeded"),
    ):
        service._call_dalle3_with_retry("a prompt", "1024x1024", "standard")

    assert client.images.generate.call_count == 3


def test_sdk_internal_retries_are_disabled():
    """SDK retries would multiply paid or quota-rejected requests."""
    service = DallE3ImageService(api_key="test-key")
    client = _client_raising(_rate_limit_error({}, "Error code: 429 - Rate limit reached"))

    with (
        patch("openai.OpenAI", return_value=client) as mock_openai,
        patch("btcedu.services.image_gen_service.time.sleep"),
        pytest.raises(RuntimeError),
    ):
        service._call_dalle3_with_retry("a prompt", "1024x1024", "standard")

    assert mock_openai.call_args.kwargs["max_retries"] == 0
