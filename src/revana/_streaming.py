"""Streamed chat completions: server-sent events read into chunks (OpenAI's shape)."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

import httpx

from ._errors import RevanaError

__all__ = ["ChatStream", "AsyncChatStream"]

Chunk = Dict[str, Any]


class _Events:
    """Collects the data lines of one server-sent event until the blank line that ends it."""

    def __init__(self) -> None:
        self._data: List[str] = []

    def feed(self, line: str) -> Optional[str]:
        line = line.rstrip("\r")
        if not line:
            return self.flush()
        if line.startswith("data:"):
            value = line[5:]
            self._data.append(value[1:] if value.startswith(" ") else value)
        return None  # comments (": keep-alive") and other fields are skipped

    def flush(self) -> Optional[str]:
        if not self._data:
            return None
        data = "\n".join(self._data)
        self._data = []
        return data


def _chunk(data: str) -> Optional[Chunk]:
    """The chunk an event carries; None at [DONE]. An error event raises RevanaError."""
    if data == "[DONE]":
        return None
    chunk = json.loads(data)
    if isinstance(chunk, dict) and chunk.get("error") and not chunk.get("choices"):
        raw = chunk["error"]
        e: Dict[str, Any] = raw if isinstance(raw, dict) else {"message": str(raw)}
        status = e.get("status")
        raise RevanaError(
            int(status) if isinstance(status, int) else 502,
            str(e.get("code") or e.get("type") or "stream_error"),
            str(e.get("message") or "The stream ended with an error"),
            chunk,
        )
    return chunk if isinstance(chunk, dict) else {"data": chunk}


class ChatStream:
    """A streamed chat completion. Iterate it for the chunks; the connection closes at the end.

    ```python
    with revana.chat.completions.create(model="gpt-5-mini", messages=msgs, stream=True) as stream:
        for chunk in stream:
            print(chunk["choices"][0]["delta"].get("content", "") if chunk["choices"] else "", end="")
    ```
    """

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        #: The request id, when the API sent one.
        self.request_id: Optional[str] = response.headers.get("x-request-id")
        self._used = False

    def __iter__(self) -> Iterator[Chunk]:
        if self._used:
            raise RuntimeError("A chat stream can be read once")
        self._used = True
        try:
            if "text/event-stream" not in self.response.headers.get("content-type", ""):
                self.response.read()  # a server that answered without streaming: its one answer
                yield self.response.json()
                return
            events = _Events()
            for line in self.response.iter_lines():
                data = events.feed(line)
                if data is None:
                    continue
                chunk = _chunk(data)
                if chunk is None:
                    return
                yield chunk
            data = events.flush()
            last = _chunk(data) if data is not None else None
            if last is not None:
                yield last
        finally:
            self.response.close()

    def close(self) -> None:
        self.response.close()

    def __enter__(self) -> "ChatStream":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncChatStream:
    """The async ChatStream: ``async for chunk in stream``."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.request_id: Optional[str] = response.headers.get("x-request-id")
        self._used = False

    async def __aiter__(self) -> AsyncIterator[Chunk]:
        if self._used:
            raise RuntimeError("A chat stream can be read once")
        self._used = True
        try:
            if "text/event-stream" not in self.response.headers.get("content-type", ""):
                await self.response.aread()
                yield self.response.json()
                return
            events = _Events()
            async for line in self.response.aiter_lines():
                data = events.feed(line)
                if data is None:
                    continue
                chunk = _chunk(data)
                if chunk is None:
                    return
                yield chunk
            data = events.flush()
            last = _chunk(data) if data is not None else None
            if last is not None:
                yield last
        finally:
            await self.response.aclose()

    async def close(self) -> None:
        await self.response.aclose()

    async def __aenter__(self) -> "AsyncChatStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
