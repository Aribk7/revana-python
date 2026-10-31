from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Literal, Optional, Union, cast, overload

import httpx

from ._base import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_AFTER,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEOUT,
    DEFAULT_WAIT_TIMEOUT,
    BaseClient,
    _quote,
    generation_body,
    list_params,
    random_key,
    read_file,
    upload_body,
)
from ._errors import RevanaError
from ._streaming import AsyncChatStream
from .types import Balance, FileInput, Generation, GenerationList, InputLike, Model, Quote, Upload

__all__ = ["AsyncRevana"]

OnUpdate = Callable[[Generation], Union[None, Awaitable[None]]]


class AsyncRevana(BaseClient):
    """The async Revana client: one API key for 200+ image, video, music and chat models.

    ```python
    from revana import AsyncRevana

    revana = AsyncRevana()  # reads REVANA_API_KEY
    video = await revana.generate(
        model="seedance-2.5",
        input={"prompt": "a corgi surfing at sunset", "duration": 5, "resolution": "720p"},
        max_cost_usd=2,
    )
    print(video["output"]["urls"][0], video["cost_usd"])
    ```
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
        max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """
        Args:
            api_key: Defaults to REVANA_API_KEY. Create one at https://revana.si/dashboard.
            base_url: Defaults to REVANA_BASE_URL, else https://api.revana.si.
            max_retries: Retries for 429, 5xx and network errors. Default 3.
            timeout: Per-request timeout in seconds. Default 60.
            max_retry_after: The longest Retry-After (seconds) an automatic retry waits for; a longer one
                is raised to you. Default 60.
            http_client: Your own httpx.AsyncClient (proxies, transports, tests).
        """
        super().__init__(api_key, base_url, max_retries, timeout, max_retry_after)
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(follow_redirects=True)
        self.generations = AsyncGenerations(self)
        self.models = AsyncModels(self)
        self.uploads = AsyncUploads(self)
        self.chat = AsyncChat(self)

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> "AsyncRevana":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def generate(
        self,
        model: str,
        input: InputLike,
        *,
        max_cost_usd: Optional[float] = None,
        max_wait_s: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_update: Optional[OnUpdate] = None,
    ) -> Generation:
        """Submit a generation and wait for the result.

        Raises GenerationFailedError if it fails (nothing is charged) and WaitTimeoutError
        after ``timeout`` seconds (default 30 minutes; it may still finish).
        """
        gen = await self.generations.create(
            model=model,
            input=input,
            max_cost_usd=max_cost_usd,
            max_wait_s=max_wait_s,
            idempotency_key=idempotency_key,
        )
        return await self.generations._wait(gen["id"], timeout, poll_interval, on_update, gen)

    async def balance(self) -> Balance:
        """What the key's account can spend."""
        return cast(Balance, await self._request("GET", "/v1/balance"))

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        return (await self._raw(method, path, **kw)).json()

    async def _raw(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        idempotency_key: Optional[str] = None,
        auth: bool = True,
        params: Optional[Dict[str, str]] = None,
        retry: Optional[bool] = None,
        new_endpoint: bool = False,
    ) -> httpx.Response:
        """One request, retrying 429, 5xx and network errors when that is safe (see _retry_wait).

        retry=True retries a POST without an idempotency key (a quote changes nothing). new_endpoint=True
        turns a 404 into EndpointNotAvailableError: the server predates that endpoint.
        """
        headers = self._headers(auth=auth, json_body=body is not None, idempotency_key=idempotency_key)
        retry_safe = self._retry_safe(method, idempotency_key) if retry is None else retry
        attempt = 0
        while True:
            try:
                res = await self._http.request(
                    method,
                    self.base_url + path,
                    headers=headers,
                    json=body,
                    params=params,
                    timeout=self.timeout,
                    follow_redirects=True,
                )
            except httpx.TransportError as exc:
                if attempt >= self.max_retries or not retry_safe:
                    raise self._network_error(exc) from exc
                await asyncio.sleep(self._backoff(attempt))
                attempt += 1
                continue
            if res.is_success:
                return res
            error = self._error_from(res, method, path, new_endpoint)
            wait = self._retry_wait(error, attempt) if retry_safe else None
            if wait is None:
                raise error
            await res.aclose()
            await asyncio.sleep(wait)
            attempt += 1

    async def _stream_chat(self, body: Dict[str, Any]) -> AsyncChatStream:
        """POST a chat with stream: true; answers once the stream has started. Never retried (billed per token)."""
        headers = self._headers(auth=True, json_body=True, idempotency_key=None, accept="text/event-stream")
        req = self._http.build_request(
            "POST", self.base_url + "/v1/chat/completions", headers=headers, json=body, timeout=self.timeout
        )
        try:
            res = await self._http.send(req, stream=True)
        except httpx.TransportError as exc:
            raise self._network_error(exc) from exc
        if not res.is_success:
            await res.aread()
            await res.aclose()
            raise self._error_from(res)
        return AsyncChatStream(res)

class AsyncGenerations:
    def __init__(self, client: AsyncRevana) -> None:
        self._client = client

    async def create(
        self,
        *,
        model: str,
        input: InputLike,
        max_cost_usd: Optional[float] = None,
        max_wait_s: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        **extra: Any,
    ) -> Generation:
        """Submit a generation. Returns at once with status "queued"; use wait() or generate().

        Always sends an Idempotency-Key (a random one unless given), reused across retries,
        so a retried request never starts a second generation.
        """
        body = generation_body(model, dict(input), max_cost_usd, max_wait_s, extra)
        key = idempotency_key or random_key()
        return cast(Generation, await self._client._request("POST", "/v1/generations", body=body, idempotency_key=key))

    async def quote(
        self,
        *,
        model: str,
        input: InputLike,
        max_cost_usd: Optional[float] = None,
        max_wait_s: Optional[int] = None,
        **extra: Any,
    ) -> Quote:
        """What a generation would cost: the same arguments as create(), worked out the same way.

        Free; nothing is created or held. Use ``quoted_usd`` as ``max_cost_usd`` on the create.
        Needs an API version deployed after 2026-10-31; an older server raises EndpointNotAvailableError.
        """
        body = generation_body(model, dict(input), max_cost_usd, max_wait_s, extra)
        return cast(
            Quote,
            await self._client._request("POST", "/v1/generations/quote", body=body, retry=True, new_endpoint=True),
        )

    async def list(
        self,
        *,
        limit: Optional[int] = None,
        before: Union[str, datetime, None] = None,
        status: Optional[str] = None,
        model: Optional[str] = None,
    ) -> GenerationList:
        """One page of the key's own generations, newest first: ``{data, has_more, next_before}``.

        Pass ``next_before`` as ``before`` for the next page, or use list_all().
        Needs an API version deployed after 2026-10-31; an older server raises EndpointNotAvailableError.
        """
        params = list_params(limit, before, status, model)
        return cast(
            GenerationList,
            await self._client._request("GET", "/v1/generations", params=params, new_endpoint=True),
        )

    async def list_all(
        self,
        *,
        limit: Optional[int] = None,
        before: Union[str, datetime, None] = None,
        status: Optional[str] = None,
        model: Optional[str] = None,
    ) -> AsyncIterator[Generation]:
        """Every generation matching the filters, newest first, fetching pages as you iterate."""
        while True:
            page = await self.list(limit=limit, before=before, status=status, model=model)
            for g in page["data"]:
                yield g
            if not page.get("has_more") or not page.get("next_before"):
                return
            before = page["next_before"]

    async def get(self, id: str) -> Generation:
        return cast(Generation, await self._client._request("GET", f"/v1/generations/{_quote(id)}"))

    async def wait(
        self,
        id: str,
        *,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_update: Optional[OnUpdate] = None,
    ) -> Generation:
        """Poll until the generation succeeds (returned) or fails (GenerationFailedError)."""
        return await self._wait(id, timeout, poll_interval, on_update, None)

    async def download(self, id: str, n: int = 1) -> bytes:
        """Download file n (1-based) of a finished generation."""
        return (await self._client._raw("GET", self._client._content_path(id, n))).content

    async def _wait(
        self,
        id: str,
        timeout: float,
        poll_interval: float,
        on_update: Optional[OnUpdate],
        first: Optional[Generation],
    ) -> Generation:
        started = time.monotonic()
        gen = first or await self.get(id)
        while True:
            if on_update:
                result = on_update(gen)
                if inspect.isawaitable(result):
                    await result
            if self._client._check_done(gen, started, timeout):
                return gen
            await asyncio.sleep(poll_interval)
            gen = await self.get(id)


class AsyncModels:
    def __init__(self, client: AsyncRevana) -> None:
        self._client = client

    async def list(self) -> List[Model]:
        """Every model you can generate with now, with its options and price.

        No key is needed; when the client has one it is sent, so the prices are the account's own price book.
        """
        return cast(List[Model], (await self._client._request("GET", "/v1/models", auth=self._client._has_key))["data"])

    async def get(self, id: str) -> Optional[Model]:
        """One model by id, or None."""
        return next((m for m in await self.list() if m["id"] == id), None)


class AsyncUploads:
    def __init__(self, client: AsyncRevana) -> None:
        self._client = client

    async def create(self, *, content_type: str, bytes: int, filename: Optional[str] = None) -> Upload:
        """Get a presigned link for a local file. Prefer upload(), which also sends the bytes."""
        body = upload_body(content_type, bytes, filename)
        return cast(Upload, await self._client._request("POST", "/v1/uploads", body=body))

    async def upload(
        self,
        data: FileInput,
        content_type: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Upload:
        """Upload bytes or a local file and get back a URL to pass as image_url / video_url /
        audio_url (good for 14 days). The content type is guessed from the filename unless given.
        Images up to 30 MB, video 50 MB, audio 15 MB. Free.
        """
        raw, content_type, filename = read_file(data, content_type, filename)
        up = await self.create(content_type=content_type, bytes=len(raw), filename=filename)
        res = await self._client._http.request(
            up.get("upload_method") or "PUT",
            up["upload_url"],
            headers=up.get("upload_headers") or {},
            content=raw,
            timeout=max(self._client.timeout, 300.0),
        )
        if not res.is_success:
            raise RevanaError(res.status_code, "upload_failed", res.text or res.reason_phrase)
        return up


class AsyncCompletions:
    def __init__(self, client: AsyncRevana) -> None:
        self._client = client

    @overload
    async def create(self, *, stream: Literal[True], **body: Any) -> AsyncChatStream: ...

    @overload
    async def create(self, *, stream: Literal[False] = ..., **body: Any) -> Dict[str, Any]: ...

    async def create(self, *, stream: bool = False, **body: Any) -> Union[AsyncChatStream, Dict[str, Any]]:
        """OpenAI-compatible chat completions: ``create(model=..., messages=[...])``.

        With ``stream=True`` it answers a AsyncChatStream of chunks (server-sent events) once the stream has started.
        Chat is billed per token and is never retried automatically.
        """
        if stream:
            return await self._client._stream_chat({**body, "stream": True})
        return cast(Dict[str, Any], await self._client._request("POST", "/v1/chat/completions", body=body))

    async def stream(self, **body: Any) -> AsyncChatStream:
        """The same as ``create(..., stream=True)``."""
        return await self._client._stream_chat({**body, "stream": True})


class AsyncChat:
    """OpenAI-compatible chat completions on the same key (90+ LLMs)."""

    def __init__(self, client: AsyncRevana) -> None:
        self.completions = AsyncCompletions(client)
