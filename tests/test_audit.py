"""Tests for the 2026-10-31 API audit follow-ups. Every call goes to an httpx MockTransport; nothing reaches the API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx
import pytest

import revana._async_client
import revana._client
from revana import (
    AsyncChatStream,
    AsyncRevana,
    ChatStream,
    EndpointNotAvailableError,
    Revana,
    RevanaError,
)
from revana._base import parse_retry_after

BASE = "https://api.test"
RID = "req_0123456789abcdef0123"
NOT_FOUND = {"error": {"code": "not_found", "message": "Not found."}}


class Mock:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        r = self.responses.pop(0)
        return r(request) if callable(r) else r


def client(mock: Mock, **kw: Any) -> Revana:
    kw.setdefault("api_key", "rv_test")
    return Revana(base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(mock)), **kw)


def aclient(mock: Mock, **kw: Any) -> AsyncRevana:
    kw.setdefault("api_key", "rv_test")
    return AsyncRevana(base_url=BASE, http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock)), **kw)


def gen(id: str, status: str = "succeeded", **kw: Any) -> Dict[str, Any]:
    g: Dict[str, Any] = {"id": id, "object": "generation", "model": "seedance-2.5", "status": status, "output": None,
                         "quoted_usd": 1.05, "cost_usd": None, "attempts": 1, "error": None,
                         "created_at": "2026-10-31T10:00:00Z", "completed_at": None}
    g.update(kw)
    return g


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> List[float]:
    slept: List[float] = []

    async def async_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(revana._client.time, "sleep", slept.append)
    monkeypatch.setattr(revana._async_client.asyncio, "sleep", async_sleep)
    return slept


# --- errors ---


def test_errors_carry_request_id_headers_and_retry_after() -> None:
    body = {"error": {"code": "insufficient_credits", "message": "top up", "request_id": RID}}
    mock = Mock(httpx.Response(402, json=body, headers={"x-request-id": RID, "retry-after": "7"}))
    with pytest.raises(RevanaError) as ei:
        client(mock).balance()
    e = ei.value
    assert (e.status, e.code, e.request_id, e.retry_after) == (402, "insufficient_credits", RID, 7)
    assert e.headers is not None and e.headers["x-request-id"] == RID
    assert f"(request {RID})" in str(e)


def test_request_id_from_the_header_or_none_before_the_deploy() -> None:
    mock = Mock(
        httpx.Response(422, json={"error": {"code": "invalid_input", "message": "no"}}, headers={"x-request-id": RID}),
        httpx.Response(422, json={"error": {"code": "invalid_input", "message": "no"}}),
    )
    c = client(mock)
    with pytest.raises(RevanaError) as first:
        c.balance()
    with pytest.raises(RevanaError) as second:
        c.balance()
    assert first.value.request_id == RID
    assert second.value.request_id is None and second.value.retry_after is None


def test_legacy_error_shape_keeps_its_code() -> None:
    mock = Mock(httpx.Response(404, json={"error": "no such job", "code": "not_found", "request_id": RID}))
    with pytest.raises(RevanaError) as ei:
        client(mock).generations.get("gen_x")
    assert (ei.value.code, ei.value.message, ei.value.request_id) == ("not_found", "no such job", RID)


def test_parse_retry_after() -> None:
    now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    assert parse_retry_after("5") == 5
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("Sat, 31 Oct 2026 10:00:30 GMT", now) == 30
    assert parse_retry_after("Sat, 31 Oct 2026 09:00:00 GMT", now) == 0


# --- retries ---


def test_concurrency_limit_without_retry_after_is_not_retried(no_sleep: List[float]) -> None:
    mock = Mock(httpx.Response(429, json={"error": {"code": "concurrency_limit", "message": "too many"}}))
    with pytest.raises(RevanaError) as ei:
        client(mock).generations.create(model="m", input={})
    assert ei.value.code == "concurrency_limit"
    assert len(mock.calls) == 1 and no_sleep == []


def test_short_retry_after_is_honoured_with_the_same_key(no_sleep: List[float]) -> None:
    mock = Mock(
        httpx.Response(429, json={"error": {"code": "concurrency_limit", "message": "x"}}, headers={"retry-after": "3"}),
        httpx.Response(202, json=gen("gen_1", "queued")),
    )
    client(mock).generations.create(model="m", input={})
    assert no_sleep == [3]
    assert mock.calls[0].headers["idempotency-key"] == mock.calls[1].headers["idempotency-key"]


# --- models, max_wait_s ---


def test_models_list_sends_the_key_when_there_is_one() -> None:
    mock = Mock(httpx.Response(200, json={"data": []}))
    client(mock, api_key="sr_live_x").models.list()
    assert mock.calls[0].headers["authorization"] == "Bearer sr_live_x"


def test_max_wait_s_is_whole_seconds() -> None:
    mock = Mock(httpx.Response(202, json=gen("gen_1", "queued")), httpx.Response(202, json=gen("gen_2", "queued")))
    c = client(mock)
    c.generations.create(model="m", input={}, max_wait_s=600)
    c.generations.create(model="m", input={}, max_wait_s=600.0)  # type: ignore[arg-type]
    assert [json.loads(r.content)["max_wait_s"] for r in mock.calls] == [600, 600]
    assert isinstance(json.loads(mock.calls[1].content)["max_wait_s"], int)
    with pytest.raises(ValueError):
        c.generations.create(model="m", input={}, max_wait_s=30.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        c.generations.create(model="m", input={}, max_wait_s=True)
    assert len(mock.calls) == 2  # refused before sending


# --- quote and list (API versions deployed after 2026-10-31) ---


def test_quote_posts_the_create_body_and_is_retried(no_sleep: List[float]) -> None:
    q = {"object": "quote", "model": "seedance-2.5", "currency": "usd", "quoted_usd": 1.25, "reserved_usd": 1.25,
         "estimated_seconds": 90}
    mock = Mock(httpx.Response(503, json={"error": {"code": "busy", "message": "x"}}, headers={"retry-after": "1"}),
                httpx.Response(200, json=q))
    got = client(mock).generations.quote(model="seedance-2.5", input={"prompt": "p", "image_url": "https://x/y.png"},
                                         max_cost_usd=2)
    assert got == q
    req = mock.calls[1]
    assert req.method == "POST" and str(req.url) == f"{BASE}/v1/generations/quote"
    assert json.loads(req.content) == {"model": "seedance-2.5", "input": {"prompt": "p", "image_url": "https://x/y.png"},
                                       "max_cost_usd": 2}
    assert "idempotency-key" not in req.headers
    assert len(mock.calls) == 2  # a quote changes nothing, so a 503 is retried


def test_quote_on_an_older_server() -> None:
    mock = Mock(httpx.Response(404, json=NOT_FOUND))
    with pytest.raises(EndpointNotAvailableError) as ei:
        client(mock).generations.quote(model="m", input={})
    e = ei.value
    assert isinstance(e, RevanaError) and e.status == 404 and e.code == "endpoint_not_available"
    assert "POST /v1/generations/quote" in str(e) and "deployed after 2026-10-31" in str(e)


def test_list_query_and_list_all_pages() -> None:
    mock = Mock(
        httpx.Response(200, json={"object": "list", "data": [gen("gen_a"), gen("gen_b")], "has_more": True,
                                  "next_before": "2026-10-30T00:00:00.000Z"}),
        httpx.Response(200, json={"object": "list", "data": [gen("gen_c")], "has_more": False, "next_before": None}),
    )
    before = datetime(2026, 10, 31, tzinfo=timezone.utc)
    ids = [g["id"] for g in client(mock).generations.list_all(limit=2, status="succeeded", model="seedance-2.5", before=before)]
    assert ids == ["gen_a", "gen_b", "gen_c"]
    assert mock.calls[0].url.path == "/v1/generations"
    assert dict(mock.calls[0].url.params) == {"limit": "2", "before": "2026-10-31T00:00:00+00:00", "status": "succeeded",
                                              "model": "seedance-2.5"}
    assert mock.calls[1].url.params["before"] == "2026-10-30T00:00:00.000Z"


def test_list_validates_status_and_reports_an_older_server() -> None:
    with pytest.raises(ValueError):
        client(Mock()).generations.list(status="done")
    mock = Mock(httpx.Response(404, json=NOT_FOUND))
    with pytest.raises(EndpointNotAvailableError, match="GET /v1/generations is not available"):
        client(mock).generations.list(limit=5)


def test_a_naive_datetime_is_sent_as_utc() -> None:
    mock = Mock(httpx.Response(200, json={"object": "list", "data": [], "has_more": False, "next_before": None}))
    client(mock).generations.list(before=datetime(2026, 10, 31, 12, 0, 0))
    assert mock.calls[0].url.params["before"] == "2026-10-31T12:00:00Z"


# --- chat streaming ---

SSE_HEADERS = {"content-type": "text/event-stream", "x-request-id": RID}


def sse(*events: Any) -> bytes:
    out = ": keep-alive\n\n"
    for e in events:
        out += f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n"
    return out.encode()


def delta(text: str) -> Dict[str, Any]:
    return {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": text}}]}


def test_chat_stream_yields_chunks() -> None:
    body = sse(delta("Hel"), delta("lo"), {"choices": [], "usage": {"cost_usd": 0.0001}}, "[DONE]")
    mock = Mock(httpx.Response(200, headers=SSE_HEADERS, content=body))
    stream = client(mock).chat.completions.create(model="x", messages=[], stream=True)
    assert isinstance(stream, ChatStream) and stream.request_id == RID
    with stream:
        chunks = list(stream)
    assert "".join(c["choices"][0]["delta"]["content"] for c in chunks if c["choices"]) == "Hello"
    assert chunks[-1]["usage"]["cost_usd"] == 0.0001
    req = mock.calls[0]
    assert req.headers["accept"] == "text/event-stream" and json.loads(req.content)["stream"] is True


def test_chat_stream_error_event_raises_and_a_refusal_raises_at_once() -> None:
    err = {"error": {"message": "The model stopped answering.", "type": "api_error", "code": "upstream_error"}}
    mock = Mock(httpx.Response(200, headers=SSE_HEADERS, content=sse(delta("a"), err, "[DONE]")),
                httpx.Response(402, json={"error": {"code": "insufficient_credits", "message": "no"}}))
    c = client(mock)
    with pytest.raises(RevanaError, match="upstream_error"):
        list(c.chat.completions.stream(model="x", messages=[]))
    with pytest.raises(RevanaError) as ei:
        c.chat.completions.create(model="x", messages=[], stream=True)
    assert ei.value.status == 402


def test_chat_without_stream_is_json_and_never_retried() -> None:
    mock = Mock(httpx.Response(502, json={"error": {"code": "upstream_error", "message": "x"}}))
    with pytest.raises(RevanaError):
        client(mock).chat.completions.create(model="x", messages=[])
    assert len(mock.calls) == 1


# --- async ---


async def test_async_quote_list_all_and_stream() -> None:
    q = {"object": "quote", "model": "m", "currency": "usd", "quoted_usd": 0.5, "reserved_usd": 0.5,
         "estimated_seconds": None}
    mock = Mock(
        httpx.Response(200, json=q),
        httpx.Response(200, json={"object": "list", "data": [gen("gen_a")], "has_more": True, "next_before": "t1"}),
        httpx.Response(200, json={"object": "list", "data": [gen("gen_b")], "has_more": False, "next_before": None}),
        httpx.Response(200, headers=SSE_HEADERS, content=sse(delta("hi"), "[DONE]")),
        httpx.Response(404, json=NOT_FOUND),
    )
    async with aclient(mock) as c:
        assert (await c.generations.quote(model="m", input={}))["quoted_usd"] == 0.5
        assert [g["id"] async for g in c.generations.list_all(limit=1)] == ["gen_a", "gen_b"]
        stream = await c.chat.completions.create(model="x", messages=[], stream=True)
        assert isinstance(stream, AsyncChatStream)
        assert [ch["choices"][0]["delta"]["content"] async for ch in stream] == ["hi"]
        with pytest.raises(EndpointNotAvailableError):
            await c.generations.list()
    assert mock.calls[2].url.params["before"] == "t1"
