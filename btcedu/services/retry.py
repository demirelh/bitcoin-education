"""Retry decorator with exponential backoff for transient errors.

Uses error classification to decide whether to retry or fail immediately.
Does not replace existing service-level retry logic (ElevenLabs, DALL-E 3, Pexels).
"""

import functools
import logging
import random
import time

from btcedu.services.errors import (
    ErrorCategory,
    PipelineError,
    classify_error,
    is_transient,
)

logger = logging.getLogger(__name__)


def retry_on_transient(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
):
    """Decorator: retry on transient errors with exponential backoff.

    - Classifies exceptions via classify_error()
    - Retries only if is_transient(category) is True
    - Permanent errors re-raised immediately
    - On final failure, wraps in PipelineError with category + suggestion

    Args:
        max_retries: Maximum number of retry attempts (total calls = max_retries + 1).
        base_delay: Initial delay in seconds before first retry.
        max_delay: Maximum delay cap in seconds.
        jitter: Add random 0-25% to delay to prevent thundering herd.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    category = classify_error(e)

                    if not is_transient(category):
                        # Permanent error — fail immediately
                        raise PipelineError(
                            str(e),
                            category,
                            original=e,
                        ) from e

                    if attempt < max_retries:
                        delay = min(base_delay * (2**attempt), max_delay)
                        if jitter:
                            delay += delay * random.uniform(0, 0.25)
                        logger.warning(
                            "%s: transient error (attempt %d/%d, %s): %s "
                            "— retrying in %.1fs",
                            func.__qualname__,
                            attempt + 1,
                            max_retries + 1,
                            category.value,
                            e,
                            delay,
                        )
                        time.sleep(delay)
                    else:
                        # Exhausted retries
                        logger.error(
                            "%s: failed after %d attempts (%s): %s",
                            func.__qualname__,
                            max_retries + 1,
                            category.value,
                            e,
                        )
                        raise PipelineError(
                            f"Failed after {max_retries + 1} attempts: {e}",
                            category,
                            original=e,
                        ) from e

            # Should not reach here, but just in case
            raise PipelineError(
                f"Failed after {max_retries + 1} attempts: {last_exc}",
                classify_error(last_exc) if last_exc else ErrorCategory.UNKNOWN,
                original=last_exc,
            )

        return wrapper

    return decorator
