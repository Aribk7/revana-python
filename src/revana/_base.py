"""What the sync and async clients share: settings, headers, retries, errors and files."""

from __future__ import annotations

import json
import mimetypes
import os
import random
import time
import uuid
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import httpx

from ._errors import GenerationFailedError, RevanaError, WaitTimeoutError
from ._version import __version__
from .types import FileInput, Generation

DEFAULT_BASE_URL = "https://api.revana.si"
DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT = 60.0
DEFAULT_WAIT_TIMEOUT = 30 * 60.0
DEFAULT_POLL_INTERVAL = 3.0
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
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("REVANA_API_KEY") or None
        self.base_url = (base_url or os.environ.get("REVANA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the key
        return f"{type(self).__name__}(base_url={self.base_url!r})"

    def _headers(self, *, auth: bool, json_body: bool, idempotency_key: Optional[str]) -> Dict[str, str]:
        headers = {"accept": "application/json", "user-agent": USER_AGENT}
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

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        """Retry-After (seconds or an HTTP date), capped at 60s; otherwise exponential backoff."""
        ra = response.headers.get("retry-after")
        if ra:
            try:
                seconds: float = float(ra)
            except ValueError:
                try:
                    seconds = parsedate_to_datetime(ra).timestamp() - time.time()
                except (TypeError, ValueError):
                    seconds = 0
            if seconds > 0:
                return min(seconds, 60.0)
        return self._backoff(attempt)

    @staticmethod
    def _is_retryable(response: httpx.Response) -> bool:
        return response.status_code == 429 or response.status_code >= 500

    @staticmethod
    def _error_from(response: httpx.Response) -> RevanaError:
        text = response.text
        body: Any = text
        try:
            body = json.loads(text)
        except ValueError:
            pass
        err = body.get("error") if isinstance(body, dict) else None
        err = err if isinstance(err, dict) else {}
        message = err.get("message") or text or response.reason_phrase
        return RevanaError(response.status_code, err.get("code") or "http_error", message, body)

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
    max_wait_s: Optional[float],
    extra: Dict[str, Any],
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"model": model, "input": input}
    if max_cost_usd is not None:
        body["max_cost_usd"] = max_cost_usd
    if max_wait_s is not None:
        body["max_wait_s"] = max_wait_s
    body.update(extra)
    return body


def upload_body(content_type: str, size: int, filename: Optional[str]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"content_type": content_type, "bytes": size}
    if filename:
        body["filename"] = filename
    return body
