from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .types import Generation

__all__ = ["RevanaError", "EndpointNotAvailableError", "GenerationFailedError", "WaitTimeoutError"]


class RevanaError(Exception):
    """An HTTP error answered by the API, e.g. 402 insufficient_credits or 422 invalid_input.

    Network failures and request timeouts that outlast the retries are raised as a
    RevanaError too, with status 0 and code "network_error" or "timeout".

    Attributes:
        headers: The response's headers (None for network errors).
        request_id: The request id (``req_`` and 20 hex digits) from ``error.request_id`` or the
            X-Request-Id header, when the API sent one (API versions deployed after 2026-10-31 do).
            Quote it when asking for help.
        retry_after: Seconds to wait before trying again, from Retry-After (429 and 503), or None.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        body: Any = None,
        *,
        headers: Optional[Mapping[str, str]] = None,
        request_id: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}" + (f" (request {request_id})" if request_id else ""))
        self.status = status
        self.code = code
        self.message = message
        self.body = body
        self.headers = headers
        self.request_id = request_id
        self.retry_after = retry_after


class EndpointNotAvailableError(RevanaError):
    """A 404 from an endpoint this API server does not have yet.

    ``generations.quote()`` and ``generations.list()`` need an API version deployed after 2026-10-31.
    """

    def __init__(self, method: str, path: str, body: Any = None, **details: Any) -> None:
        super().__init__(
            404,
            "endpoint_not_available",
            f"{method} {path} is not available on this API server; it needs an API version deployed after 2026-10-31",
            body,
            **details,
        )


class GenerationFailedError(Exception):
    """A generation that ended in status "failed". Nothing is charged for it."""

    def __init__(self, generation: Generation) -> None:
        err: Dict[str, Any] = dict(generation.get("error") or {})
        self.generation = generation
        self.code: str = err.get("code") or "unknown"
        self.retryable: bool = bool(err.get("retryable", False))
        message: Optional[str] = err.get("message")
        super().__init__(f"Generation {generation.get('id')} failed: {self.code}: {message or ''}".strip())


class WaitTimeoutError(Exception):
    """Waiting for a generation took longer than the wait timeout. It may still finish."""

    def __init__(self, generation: Generation, timeout: float) -> None:
        super().__init__(f"Generation {generation.get('id')} still {generation.get('status')} after {round(timeout)}s")
        self.generation = generation
