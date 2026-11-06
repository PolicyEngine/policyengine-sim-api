"""Retry a Stage 12 dataset load after a transient download failure.

A Stage 12 simulation that selects a dataset other than the image's default
(for example the UK local-area dataset) without a baked copy loads it into a
temporary folder, so each new container downloads it from Hugging Face, and
policyengine.py makes a single request for each file. This retries the load
a bounded number of times, with a jittered wait that doubles each time.

policyengine.py writes each download to a temporary file, checks its SHA-256
and only then moves it into place, removing the temporary file on any
failure. A retry therefore downloads again any file that did not complete,
and reuses only files whose SHA-256 matched.

This is Stage 12's own copy of the main path's retry
(``policyengine_simulation_executor.dataset_download_retry``): the two paths
share no retry code.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

MAXIMUM_LOAD_ATTEMPTS = 3
INITIAL_RETRY_DELAY_SECONDS = 2.0
RETRY_DELAY_GROWTH_FACTOR = 2.0
_FIRST_SERVER_ERROR_STATUS = 500


@dataclass(frozen=True)
class RetryTiming:
    """The sleep and jitter a Stage 12 dataset load retry uses; tests replace them.

    ``choose_delay`` picks a delay between its lower and upper bound.
    """

    sleep: Callable[[float], None] = time.sleep
    choose_delay: Callable[[float, float], float] = random.uniform


# The timing a load uses when it is given none; tests replace it.
DEFAULT_RETRY_TIMING = RetryTiming()


def is_transient_download_failure(error: requests.RequestException) -> bool:
    """Return whether a later attempt could download what this attempt could not.

    Transient: a connection error (including a read timeout while the body
    streams, which requests raises as a ConnectionError), a timeout before a
    response, a body cut off mid-transfer, and a 5xx response. policyengine.py
    raises ``DatasetMaterializationError``, not a requests error, for 401, 403
    and 404 responses and for a SHA-256 mismatch, so those never reach this
    check; any other 4xx response, including a 429 rate limit, is not
    transient either. A TLS failure, certificate verification included, is a
    ConnectionError, so it is retried like any other connection failure.
    """
    if isinstance(error, requests.HTTPError):
        response = error.response
        return (
            response is not None and response.status_code >= _FIRST_SERVER_ERROR_STATUS
        )
    return isinstance(
        error,
        (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    )


def load_with_download_retry[LoadedValue](
    load: Callable[[], LoadedValue],
    *,
    dataset_name: str,
    timing: RetryTiming | None = None,
) -> LoadedValue:
    """Return ``load()``, calling it again after a transient download failure.

    At most ``MAXIMUM_LOAD_ATTEMPTS`` attempts are made; the last attempt's
    error propagates. Any error that is not a transient download failure
    propagates at once.
    """
    timing = timing or DEFAULT_RETRY_TIMING
    delay_ceiling = INITIAL_RETRY_DELAY_SECONDS
    attempt = 1
    while True:
        try:
            return load()
        except requests.RequestException as error:
            if attempt >= MAXIMUM_LOAD_ATTEMPTS or not is_transient_download_failure(
                error
            ):
                raise
            delay = timing.choose_delay(delay_ceiling / 2, delay_ceiling)
            logger.warning(
                "Stage 12 download of dataset %s failed on attempt %d of %d; "
                "retrying after %.1f seconds",
                dataset_name,
                attempt,
                MAXIMUM_LOAD_ATTEMPTS,
                delay,
                exc_info=error,
            )
            timing.sleep(delay)
            attempt += 1
            delay_ceiling *= RETRY_DELAY_GROWTH_FACTOR
