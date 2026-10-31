from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Union

import httpx
import pytest

import revana._async_client
import revana._client
from revana import AsyncRevana, GenerationFailedError, Revana, RevanaError, WaitTimeoutError

BASE = "https://api.test"
Responder = Union[httpx.Response, Callable[[httpx.Request], httpx.Response]]


def gen(status: str, **kw: Any) -> Dict[str, Any]:
    g: Dict[str, Any] = {
        "id": "gen_1",
        "object": "generation",
        "model": "seedance-2.5",
        "status": status,
        "output": None,
        "quoted_usd": 0.5,
        "cost_usd": None,
        "attempts": 1,
        "error": None,
        "created_at": "2026-10-30T00:00:00Z",
        "completed_at": None,
    }
    g.update(kw)
    return g


def done(**kw: Any) -> Dict[str, Any]:
    return gen("succeeded", output={"urls": ["https://cdn.test/v.mp4"]}, cost_usd=0.5, **kw)


class Mock:
    """A queue of responses that records every request."""

    def __init__(self, *responses: Responder) -> None:
        self.responses: List[Responder] = list(responses)
        self.calls: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        r = self.responses.pop(0)
        return r(request) if callable(r) else r


def client(mock: Mock, **kw: Any) -> Revana:
    kw.setdefault("api_key", "rv_test")
    return Revana(base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(mock)), **kw)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> List[float]:
    slept: List[float] = []

    async def async_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(revana._client.time, "sleep", slept.append)
    monkeypatch.setattr(revana._async_client.asyncio, "sleep", async_sleep)
    return slept


def test_auth_and_idempotency_headers() -> None:
    mock = Mock(httpx.Response(202, json=gen("queued")))
    g = client(mock).generations.create(model="seedance-2.5", input={"prompt": "hi"}, max_cost_usd=2)
    assert g["id"] == "gen_1"
    req = mock.calls[0]
    assert req.method == "POST" and str(req.url) == f"{BASE}/v1/generations"
    assert req.headers["authorization"] == "Bearer rv_test"
    assert req.headers["user-agent"] == "revana-python/0.1.0"
    assert len(req.headers["idempotency-key"]) == 36  # uuid4
    assert json.loads(req.content) == {"model": "seedance-2.5", "input": {"prompt": "hi"}, "max_cost_usd": 2}


def test_given_idempotency_key_is_sent() -> None:
    mock = Mock(httpx.Response(202, json=gen("queued")))
    client(mock).generations.create(model="m", input={}, idempotency_key="my-key")
    assert mock.calls[0].headers["idempotency-key"] == "my-key"


def test_key_and_base_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVANA_API_KEY", "rv_env")
    monkeypatch.setenv("REVANA_BASE_URL", "https://env.test/")
    mock = Mock(httpx.Response(200, json={"object": "balance", "available_usd": 1}))
    c = Revana(http_client=httpx.Client(transport=httpx.MockTransport(mock)))
    c.balance()
    assert str(mock.calls[0].url) == "https://env.test/v1/balance"
    assert mock.calls[0].headers["authorization"] == "Bearer rv_env"
    assert "rv_env" not in repr(c)


def test_generate_polls_to_success(no_sleep: List[float]) -> None:
    mock = Mock(
        httpx.Response(202, json=gen("queued")),
        httpx.Response(200, json=gen("running")),
        httpx.Response(200, json=done()),
    )
    seen: List[str] = []
    g = client(mock).generate(
        model="seedance-2.5", input={"prompt": "a corgi"}, poll_interval=2, on_update=lambda g: seen.append(g["status"])
    )
    assert g["status"] == "succeeded" and g["output"]["urls"] == ["https://cdn.test/v.mp4"]
    assert seen == ["queued", "running", "succeeded"]
    assert [c.method for c in mock.calls] == ["POST", "GET", "GET"]
    assert str(mock.calls[1].url) == f"{BASE}/v1/generations/gen_1"
    assert no_sleep == [2, 2]


def test_failed_generation_raises() -> None:
    err = {"code": "content_policy", "message": "refused", "retryable": False}
    mock = Mock(httpx.Response(202, json=gen("queued")), httpx.Response(200, json=gen("failed", error=err, cost_usd=0)))
    with pytest.raises(GenerationFailedError) as ei:
        client(mock).generate(model="m", input={"prompt": "x"})
    assert ei.value.code == "content_policy"
    assert ei.value.retryable is False
    assert ei.value.generation["cost_usd"] == 0
    assert "gen_1 failed: content_policy: refused" in str(ei.value)


def test_wait_timeout() -> None:
    mock = Mock(httpx.Response(200, json=gen("running")))
    with pytest.raises(WaitTimeoutError) as ei:
        client(mock).generations.wait("gen_1", timeout=0)
    assert ei.value.generation["status"] == "running"


def test_429_retried_with_same_idempotency_key(no_sleep: List[float]) -> None:
    mock = Mock(
        httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"code": "rate_limited", "message": "slow"}}),
        httpx.Response(503, text="busy"),
        httpx.Response(202, json=gen("queued")),
    )
    client(mock).generations.create(model="m", input={})
    keys = {c.headers["idempotency-key"] for c in mock.calls}
    assert len(mock.calls) == 3 and len(keys) == 1
    assert no_sleep[0] == 2  # Retry-After honored
    assert 1.0 <= no_sleep[1] < 1.25  # then backoff 0.5 * 2^1 + jitter


def test_retry_after_over_the_limit_is_raised_not_cut_short(no_sleep: List[float]) -> None:
    mock = Mock(httpx.Response(429, headers={"retry-after": "600"}))
    with pytest.raises(RevanaError) as ei:
        client(mock).balance()
    assert ei.value.retry_after == 600
    assert no_sleep == [] and len(mock.calls) == 1  # never retried sooner than the server asked

    mock = Mock(httpx.Response(429, headers={"retry-after": "600"}), httpx.Response(200, json={"available_usd": 1}))
    client(mock, max_retry_after=900).balance()
    assert no_sleep == [600]


def test_post_without_idempotency_key_not_retried() -> None:
    mock = Mock(httpx.Response(503, json={"error": {"code": "unavailable", "message": "down"}}))
    with pytest.raises(RevanaError) as ei:
        client(mock).chat.completions.create(model="gpt", messages=[])
    assert ei.value.status == 503 and len(mock.calls) == 1


def test_retries_give_up(no_sleep: List[float]) -> None:
    mock = Mock(*[httpx.Response(500, text="boom") for _ in range(3)])
    with pytest.raises(RevanaError) as ei:
        client(mock, max_retries=2).balance()
    assert ei.value.status == 500 and ei.value.code == "http_error" and ei.value.message == "boom"
    assert len(mock.calls) == 3


def test_network_error_retried_then_wrapped() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    mock = Mock(boom, httpx.Response(200, json={"available_usd": 3}))
    assert client(mock).balance()["available_usd"] == 3

    mock = Mock(boom, boom)
    with pytest.raises(RevanaError) as ei:
        client(mock, max_retries=1).balance()
    assert ei.value.status == 0 and ei.value.code == "network_error"


def test_402_becomes_revana_error() -> None:
    body = {"error": {"code": "insufficient_credits", "message": "Top up at revana.si"}}
    mock = Mock(httpx.Response(402, json=body))
    with pytest.raises(RevanaError) as ei:
        client(mock).generations.create(model="m", input={})
    e = ei.value
    assert (e.status, e.code, e.message, e.body) == (402, "insufficient_credits", "Top up at revana.si", body)
    assert str(e) == "402 insufficient_credits: Top up at revana.si"
    assert len(mock.calls) == 1


def test_models_list_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVANA_API_KEY", raising=False)
    models = [{"id": "seedance-2.5", "object": "model", "kind": "video"}, {"id": "nano-banana-pro", "object": "model"}]
    mock = Mock(httpx.Response(200, json={"object": "list", "data": models}), httpx.Response(200, json={"data": models}))
    c = Revana(base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(mock)))
    assert [m["id"] for m in c.models.list()] == ["seedance-2.5", "nano-banana-pro"]
    assert "authorization" not in mock.calls[0].headers
    assert c.models.get("seedance-2.5") == models[0]


def test_missing_key_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVANA_API_KEY", raising=False)
    mock = Mock()
    c = Revana(base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(mock)))
    for call in (c.balance, lambda: c.generate(model="m", input={})):
        with pytest.raises(RevanaError) as ei:
            call()
        assert ei.value.code == "missing_api_key" and ei.value.status == 401
    assert mock.calls == []


def test_upload_puts_bytes(tmp_path: Any) -> None:
    upload = {
        "id": "upl_1",
        "object": "upload",
        "url": f"{BASE}/v1/uploads/upl_1",
        "upload_url": "https://storage.test/put?sig=abc",
        "upload_method": "PUT",
        "upload_headers": {"content-type": "image/png", "x-amz-meta": "1"},
        "content_type": "image/png",
        "bytes": 4,
    }
    path = tmp_path / "frame.png"
    path.write_bytes(b"\x89PNG")
    mock = Mock(httpx.Response(201, json=upload), httpx.Response(200))
    up = client(mock).uploads.upload(path)
    assert up["url"] == f"{BASE}/v1/uploads/upl_1"
    create, put = mock.calls
    assert json.loads(create.content) == {"content_type": "image/png", "bytes": 4, "filename": "frame.png"}
    assert put.method == "PUT" and str(put.url) == "https://storage.test/put?sig=abc"
    assert put.content == b"\x89PNG"
    assert put.headers["content-type"] == "image/png" and put.headers["x-amz-meta"] == "1"
    assert "authorization" not in put.headers  # never leak the key to storage


def test_upload_bytes_guesses_type_and_reports_put_failure() -> None:
    upload = {"id": "upl_1", "url": "u", "upload_url": "https://storage.test/p", "upload_headers": {}}
    mock = Mock(httpx.Response(201, json=upload), httpx.Response(403, text="SignatureDoesNotMatch"))
    with pytest.raises(RevanaError) as ei:
        client(mock).uploads.upload(b"abc", filename="clip.mp4")
    assert json.loads(mock.calls[0].content)["content_type"] == "video/mp4"
    assert (ei.value.status, ei.value.code) == (403, "upload_failed")
    with pytest.raises(ValueError):
        client(Mock()).uploads.upload(b"abc")


def test_download_follows_redirect() -> None:
    mock = Mock(
        httpx.Response(302, headers={"location": "https://cdn.test/v.mp4"}),
        httpx.Response(200, content=b"video-bytes"),
    )
    data = client(mock).generations.download("gen_1", 2)
    assert data == b"video-bytes"
    assert str(mock.calls[0].url) == f"{BASE}/v1/generations/gen_1/content/2"
    assert "authorization" not in mock.calls[1].headers  # httpx drops it on a cross-origin redirect


async def test_async_happy_path(tmp_path: Any) -> None:
    mock = Mock(
        httpx.Response(202, json=gen("queued")),
        httpx.Response(200, json=gen("running")),
        httpx.Response(200, json=done()),
        httpx.Response(200, content=b"mp4"),
        httpx.Response(200, json={"object": "balance", "available_usd": 9.5}),
        httpx.Response(200, json={"data": [{"id": "veo-3.1-fast", "object": "model"}]}),
    )
    updates: List[str] = []

    async def on_update(g: Any) -> None:
        updates.append(g["status"])

    async with AsyncRevana(
        api_key="rv_test", base_url=BASE, http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock))
    ) as c:
        g = await c.generate(model="seedance-2.5", input={"prompt": "x"}, on_update=on_update)
        assert g["cost_usd"] == 0.5
        assert await c.generations.download(g["id"]) == b"mp4"
        assert (await c.balance())["available_usd"] == 9.5
        assert (await c.models.get("veo-3.1-fast")) is not None
    assert updates == ["queued", "running", "succeeded"]
    assert mock.calls[0].headers["idempotency-key"]
    assert mock.calls[0].headers["user-agent"] == "revana-python/0.1.0"


async def test_async_retry_and_error() -> None:
    mock = Mock(
        httpx.Response(429),
        httpx.Response(402, json={"error": {"code": "insufficient_credits", "message": "no"}}),
    )
    c = AsyncRevana(api_key="k", base_url=BASE, http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock)))
    with pytest.raises(RevanaError) as ei:
        await c.generations.create(model="m", input={})
    assert ei.value.code == "insufficient_credits"
    assert mock.calls[0].headers["idempotency-key"] == mock.calls[1].headers["idempotency-key"]
