"""What the sync and async clients share: settings, headers, retries, errors and files."""

from __future__ import annotations

import json
import mimetypes
import os
import random
import time
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional, Tuple, Union
from urllib.parse import quote

import httpx

from ._errors import EndpointNotAvailableError, GenerationFailedError, RevanaError, WaitTimeoutError
from ._version import __version__
from .types import FileInput, Generation

DEFAULT_BASE_URL = "https://api.revana.si"
DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT = 60.0
DEFAULT_WAIT_TIMEOUT = 30 * 60.0
DEFAULT_POLL_INTERVAL = 3.0
#: The longest Retry-After an automatic retry waits for; a longer one is raised to the caller.
DEFAULT_MAX_RETRY_AFTER = 60.0
USER_AGENT = f"revana-python/{__version__}"

MIME: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}


def random_key() -> str:
    return str(uuid.uuid4())


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Seconds from a Retry-After header: a number of seconds or an HTTP date. None if absent or unreadable."""
    if value is None or not value.strip():
        return None
    v = value.strip()
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        at = parsedate_to_datetime(v).timestamp()
    except (TypeError, ValueError, IndexError):
        return None
    return max(0.0, float(int(at - (time.time() if now is None else now) + 0.999)))


def guess_content_type(filename: Optional[str]) -> Optional[str]:
    """The content type of a file name: the common media types first, then the mimetypes table."""
    if not filename:
        return None
    ext = os.path.splitext(filename)[1].lower()
    return MIME.get(ext) or mimetypes.guess_type(filename)[0]


def read_file(data: FileInput, content_type: Optional[str], filename: Optional[str]) -> Tuple[bytes, str, Optional[str]]:
    """Turn bytes or a path into (bytes, content type, filename)."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
    else:
        path = os.fspath(data)
        with open(path, "rb") as f:
            raw = f.read()
        filename = filename or os.path.basename(path)
    content_type = content_type or guess_content_type(filename)
    if not content_type:
        raise ValueError("Cannot tell the content type: pass content_type, or a filename with a known extension")
    return raw, content_type, filename


class BaseClient:
    base_url: str
    max_retries: int
    timeout: float

    def __init__(
        self,
        api_key: Optional[str],
        base_url: Optional[str],
        max_retries: int,
        timeout: float,
        max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("REVANA_API_KEY") or None
        self.base_url = (base_url or os.environ.get("REVANA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_retry_after = max_retry_after

    def __repr__(self) -> str:  # never show the key
        return f"{type(self).__name__}(base_url={self.base_url!r})"

    @property
    def _has_key(self) -> bool:
        return bool(self._api_key)

    def _headers(
        self, *, auth: bool, json_body: bool, idempotency_key: Optional[str], accept: str = "application/json"
    ) -> Dict[str, str]:
        headers = {"accept": accept, "user-agent": USER_AGENT}
        if auth:
            if not self._api_key:
                raise RevanaError(
                    401,
                    "missing_api_key",
                    "Set REVANA_API_KEY or pass api_key. Create one at https://revana.si/dashboard",
                )
            headers["authorization"] = f"Bearer {self._api_key}"
        if json_body:
            headers["content-type"] = "application/json"
        if idempotency_key:
            headers["idempotency-key"] = idempotency_key
        return headers

    @staticmethod
    def _retry_safe(method: str, idempotency_key: Optional[str]) -> bool:
        """GETs are always safe to retry; a POST only with an idempotency key."""
        return method == "GET" or bool(idempotency_key)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(0.5 * 2.0**attempt, 8.0) + random.random() * 0.25

    def _retry_wait(self, error: RevanaError, attempt: int) -> Optional[float]:
        """Seconds to wait before retrying this error, or None to raise it.

        Only 429 and 5xx are retried, never sooner than their Retry-After, and never when Retry-After is
        longer than max_retry_after. A 429 concurrency_limit without one is raised at once: a slot frees
        only when one of the account's generations finishes.
        """
        if attempt >= self.max_retries or not (error.status == 429 or error.status >= 500):
            return None
        if error.retry_after is not None:
            return error.retry_after if error.retry_after <= self.max_retry_after else None
        if error.code == "concurrency_limit":
            return None
        return self._backoff(attempt)

    @staticmethod
    def _error_from(response: httpx.Response, method: str = "", path: str = "", new_endpoint: bool = False) -> RevanaError:
        """A RevanaError from an error answer: {error: {code, message, request_id}}, or the older {error, code}."""
        text = response.text
        body: Any = text
        try:
            body = json.loads(text)
        except ValueError:
            pass
        raw = body.get("error") if isinstance(body, dict) else None
        err: Dict[str, Any] = raw if isinstance(raw, dict) else {}
        top: Dict[str, Any] = body if isinstance(body, dict) else {}
        code = err.get("code") or top.get("code") or "http_error"
        message = err.get("message") or (raw if isinstance(raw, str) else None) or text or response.reason_phrase
        request_id = err.get("request_id") or top.get("request_id") or response.headers.get("x-request-id")
        details: Dict[str, Any] = {
            "headers": response.headers,
            "request_id": request_id if isinstance(request_id, str) else None,
            "retry_after": parse_retry_after(response.headers.get("retry-after")),
        }
        if new_endpoint and response.status_code == 404:
            return EndpointNotAvailableError(method, path.split("?")[0], body, **details)
        return RevanaError(response.status_code, str(code), str(message), body, **details)

    @staticmethod
    def _network_error(exc: httpx.TransportError) -> RevanaError:
        code = "timeout" if isinstance(exc, httpx.TimeoutException) else "network_error"
        return RevanaError(0, code, str(exc) or type(exc).__name__)

    @staticmethod
    def _content_path(id: str, n: int) -> str:
        return f"/v1/generations/{_quote(id)}/content" + (f"/{n}" if n > 1 else "")

    @staticmethod
    def _check_done(gen: Generation, started: float, timeout: float) -> bool:
        """True once succeeded; raises once failed or out of time; False to keep waiting."""
        if gen.get("status") == "succeeded":
            return True
        if gen.get("status") == "failed":
            raise GenerationFailedError(gen)
        if time.monotonic() - started >= timeout:
            raise WaitTimeoutError(gen, timeout)
        return False


def _quote(id: str) -> str:
    return quote(id, safe="")


def generation_body(
    model: str,
    input: Dict[str, Any],
    max_cost_usd: Optional[float],
    max_wait_s: Optional[int],
    extra: Dict[str, Any],
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"model": model, "input": input}
    if max_cost_usd is not None:
        body["max_cost_usd"] = max_cost_usd
    if max_wait_s is not None:
        body["max_wait_s"] = whole_seconds(max_wait_s)
    body.update(extra)
    return body


def whole_seconds(value: Any) -> int:
    """max_wait_s is a whole number of seconds: 30 and 30.0 pass, 30.5 and True do not."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("max_wait_s must be a whole number of seconds")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("max_wait_s must be a whole number of seconds")
        return int(value)
    return value


#: The statuses generations.list() filters on.
STATUSES = ("queued", "running", "succeeded", "failed")


def list_params(
    limit: Optional[int], before: Union[str, datetime, None], status: Optional[str], model: Optional[str]
) -> Dict[str, str]:
    """The query of GET /v1/generations. A datetime before is sent as ISO 8601 (a naive one is taken as UTC)."""
    q: Dict[str, str] = {}
    if limit is not None:
        q["limit"] = str(limit)
    if before is not None:
        if isinstance(before, datetime):
            iso = before.isoformat()
            q["before"] = iso if before.tzinfo else iso + "Z"
        else:
            q["before"] = before
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        q["status"] = status
    if model is not None:
        q["model"] = model
    return q


def upload_body(content_type: str, size: int, filename: Optional[str]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"content_type": content_type, "bytes": size}
    if filename:
        body["filename"] = filename
    return body
