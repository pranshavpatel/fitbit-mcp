"""Read-only HTTP client (GET only). Ported from the exporter's Client and improved:
rate-limit and back-off waits go through a cancellable `waiter` supplied by the job,
so MCP calls never block and cancellation takes effect during long waits."""
from __future__ import annotations

import os
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from . import catalog
from .oauth import AuthError, OAuthSession, provider_error


class ApiError(Exception):
    def __init__(self, status: int, reason: str):
        self.status, self.reason = status, reason
        super().__init__("HTTP {}: {}".format(status, reason))


class RateLimitExhausted(Exception):
    def __init__(self, resume_after: float):
        self.resume_after = resume_after
        super().__init__("Rate limit reached; resume after {} seconds".format(int(resume_after)))


class NetworkError(Exception):
    pass


def checked_url(base: str, path: str) -> str:
    """Never forward bearer tokens to a host specified by untrusted response data."""
    url = path if path.startswith("https://") else base + path
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != urlsplit(base).netloc
            or parsed.username or parsed.password or parsed.fragment):
        raise ApiError(0, "Rejected an unexpected API URL")
    return url


def retry_delay(headers: Any, fallback: float) -> float:
    value = headers.get("Retry-After")
    if value:
        try:
            return max(1.0, float(value))
        except ValueError:
            try:
                return max(1.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (ValueError, TypeError):
                pass
    try:
        return max(1.0, float(headers.get("fitbit-rate-limit-reset", fallback)) + 1)
    except (TypeError, ValueError):
        return fallback


# Rate-limit windows are per provider account, so they are shared by all jobs in this process.
_NOT_BEFORE: dict[str, float] = {}
_NOT_BEFORE_LOCK = threading.Lock()


def default_waiter(seconds: float, reason: str) -> None:  # pragma: no cover - used outside jobs
    time.sleep(seconds)


class ApiClient:
    def __init__(self, session: OAuthSession, waiter: Callable[[float, str], None] = default_waiter,
                 http: Any = None, max_wait: float = 3 * 3600, wait: bool = True):
        self.session, self.waiter, self.max_wait, self.wait = session, waiter, max_wait, wait
        self.http = http or requests.Session()
        self.provider = session.provider
        self.base = session.base
        # Politeness gap between requests (health-coach-app uses 0.2 s for Google).
        self.min_interval = float(os.environ.get("FITBIT_MCP_MIN_REQUEST_INTERVAL", "0.2")) \
            if self.provider == "google" else 0.0
        self._last_request = 0.0

    def _not_before(self) -> float:
        with _NOT_BEFORE_LOCK:
            return _NOT_BEFORE.get(self.provider, 0.0)

    def _set_not_before(self, moment: float) -> None:
        with _NOT_BEFORE_LOCK:
            _NOT_BEFORE[self.provider] = max(_NOT_BEFORE.get(self.provider, 0.0), moment)

    def _pause_until(self, moment: float, reason: str) -> None:
        delay = moment - time.time()
        if delay <= 0:
            return
        if not self.wait or delay > self.max_wait:
            raise RateLimitExhausted(delay)
        self.waiter(delay, reason)

    def get(self, path: str, params: dict | None = None, raw: bool = False) -> Any:
        return self._request("GET", path, params=params, raw=raw)

    def rollup(self, path: str, body: dict) -> Any:
        """Google `dataPoints:dailyRollUp`: a read-only aggregate query that the API exposes as POST.

        This is the only non-GET request the client can make; any other path is refused.
        """
        if self.provider != "google" or not path.endswith("/dataPoints:dailyRollUp"):
            raise ApiError(0, "Only Google dailyRollUp queries may use POST")
        return self._request("POST", path, body=body)

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_request
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_request = time.monotonic()

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                 raw: bool = False) -> Any:
        url = checked_url(self.base, path)
        refreshed = False
        headers_extra = {"Accept-Language": catalog.FITBIT_LOCALE} if self.provider == "fitbit" else {}
        for attempt in range(7):
            self._pause_until(self._not_before(), "provider rate limit")
            bearer = self.session.bearer()
            self._throttle()
            headers = dict(headers_extra, Authorization="Bearer " + bearer)
            try:
                if method == "GET":
                    response = self.http.get(url, params=params, timeout=(15, 90), allow_redirects=False,
                                             headers=headers)
                else:
                    response = self.http.post(url, json=body, timeout=(15, 90), allow_redirects=False,
                                              headers=headers)
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 6:
                    raise NetworkError("Network failed repeatedly; resume the job later.") from None
                self.waiter(min(2 ** attempt, 30), "network retry back-off")
                continue
            status = response.status_code
            if status == 401 and not refreshed:
                self.session.force_refresh(bearer)
                refreshed = True
                continue
            if status == 429:
                fallback = 3600 if self.provider == "fitbit" else 60
                self._set_not_before(time.time() + retry_delay(response.headers, fallback))
                if attempt == 6:
                    raise RateLimitExhausted(retry_delay(response.headers, fallback))
                continue
            if (status >= 500 or status == 408) and attempt < 6:
                self.waiter(min(2 ** attempt, 30), "provider server error back-off")
                continue
            if not 200 <= status < 300:
                raise ApiError(status, provider_error(response))
            if response.headers.get("fitbit-rate-limit-remaining") == "0":
                self._set_not_before(time.time() + retry_delay(response.headers, 3600))
            if raw:
                return response.content
            try:
                return response.json()
            except ValueError:
                raise ApiError(status, "invalid JSON in response") from None
        raise NetworkError("Request retry limit reached.")


__all__ = ["ApiClient", "ApiError", "AuthError", "NetworkError", "RateLimitExhausted", "checked_url",
           "retry_delay"]
