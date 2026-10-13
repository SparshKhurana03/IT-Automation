"""HTTP client with retry/backoff shared by the Graph and ServiceDesk clients."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from helpers.errors import ApiError

LOG = logging.getLogger("onboarding.http")


class RetryHttpClient:
    def __init__(self, retry_config: dict[str, Any] | None = None, session: requests.Session | None = None):
        retry_config = retry_config or {}
        self.session = session or requests.Session()
        self.maximum_attempts = int(retry_config.get("maximum_attempts", 4))
        self.retryable = set(retry_config.get("retryable_http_status_codes", [429, 500, 502, 503, 504]))
        self.initial_delay = float(retry_config.get("initial_delay_seconds", 10))
        self.multiplier = float(retry_config.get("backoff_multiplier", 3))
        self.maximum_delay = float(retry_config.get("maximum_delay_seconds", 300))
        self.honor_retry_after = bool(retry_config.get("honor_retry_after_header", True))
        self.timeout = (10, int(retry_config.get("read_timeout_seconds", 60)))

    def request(
        self,
        method: str,
        url: str,
        *,
        expected: set[int],
        extra_retryable: set[int] | None = None,
        idempotent: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        """Send a request, retrying transient failures.

        ``extra_retryable`` adds status codes that are retryable for this call
        only. It is used for calls against a user created seconds ago, where
        Graph can briefly answer 404 until the object has replicated.

        ``idempotent=False`` (create user, add note) only retries throttling
        (429): after a timeout or 5xx the server may already have done the
        work, and a blind retry would create a duplicate.
        """
        retryable = self.retryable | (extra_retryable or set())
        if not idempotent:
            retryable = {429} | (extra_retryable or set())
        delay = self.initial_delay
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == self.maximum_attempts or not idempotent:
                    raise ApiError(f"Network error calling {method} {url}: {exc}", retryable=True) from exc
                LOG.warning("Network error (%s). Retrying in %.0f seconds", exc.__class__.__name__, delay)
                time.sleep(delay)
                delay = min(delay * self.multiplier, self.maximum_delay)
                continue

            if response.status_code in expected:
                return response

            if response.status_code not in retryable or attempt == self.maximum_attempts:
                request_id = response.headers.get("request-id") or response.headers.get("client-request-id")
                raise ApiError(
                    f"{method} {url} failed with HTTP {response.status_code}; "
                    f"request-id={request_id}; response={response.text[:1000]}",
                    status_code=response.status_code,
                    retryable=response.status_code in retryable,
                )

            retry_after = response.headers.get("Retry-After")
            if self.honor_retry_after and retry_after and retry_after.isdigit():
                sleep_for = min(float(retry_after), self.maximum_delay)
            else:
                sleep_for = delay
            LOG.warning("HTTP %s from %s %s. Retrying in %.0f seconds", response.status_code, method, url, sleep_for)
            time.sleep(sleep_for)
            delay = min(delay * self.multiplier, self.maximum_delay)

        raise ApiError("HTTP retry loop exited unexpectedly")
