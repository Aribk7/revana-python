from __future__ import annotations

import json
from typing import Any, List

import httpx
import pytest

import revana.cli as cli
from revana import Revana


def run(monkeypatch: pytest.MonkeyPatch, handler: Any, argv: List[str]) -> None:
    real = Revana

    def make() -> Revana:
        return real(api_key="k", base_url="https://api.test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(cli, "Revana", make)
    monkeypatch.setattr("revana._client.time.sleep", lambda s: None)
    cli.main(argv)


def test_generate_uploads_local_image_and_saves(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: Any) -> None:
    img = tmp_path / "start.jpg"
    img.write_bytes(b"jpg")
    out = tmp_path / "out.mp4"
    calls: List[httpx.Request] = []
    finished = {
        "id": "gen_9",
        "status": "succeeded",
        "output": {"urls": ["https://cdn.test/a.mp4"]},
        "cost_usd": 1.2,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        path = req.url.path
        if path == "/v1/uploads":
            return httpx.Response(201, json={"id": "upl", "url": "https://api.test/v1/uploads/upl", "upload_url": "https://s.test/p", "upload_headers": {}})
        if req.url.host == "s.test":
            return httpx.Response(200)
        if path == "/v1/generations":
            return httpx.Response(202, json={**finished, "status": "queued", "output": None})
        if path == "/v1/generations/gen_9":
            return httpx.Response(200, json=finished)
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "seedance-2.5", "kind": "video"}]})
        if path == "/v1/generations/gen_9/content":
            return httpx.Response(200, content=b"video")
        raise AssertionError(path)

    run(
        monkeypatch,
        handler,
        ["generate", "seedance-2.5", "a", "corgi", "--duration", "5", "--resolution", "720p", "--aspect", "9:16",
         "--audio", "--image", str(img), "--max-cost", "2", "--set", "seed=7", "-o", str(out)],
    )
    body = json.loads(next(c for c in calls if c.url.path == "/v1/generations").content)
    assert body == {
        "model": "seedance-2.5",
        "input": {"prompt": "a corgi", "duration": 5, "resolution": "720p", "aspect_ratio": "9:16", "audio": True,
                  "image_url": "https://api.test/v1/uploads/upl", "seed": 7},
        "max_cost_usd": 2.0,
    }
    assert out.read_bytes() == b"video"
    captured = capsys.readouterr()
    assert str(out) in captured.out and "cost $1.2" in captured.err


def test_errors_exit_1(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    handler = lambda req: httpx.Response(401, json={"error": {"code": "unauthorized", "message": "bad key"}})
    with pytest.raises(SystemExit) as ei:
        run(monkeypatch, handler, ["balance"])
    assert ei.value.code == 1
    assert "401 unauthorized: bad key" in capsys.readouterr().err


def test_models_table(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    models = [
        {"id": "seedance-2.5", "kind": "video", "price": {"unit": "per_second", "usd": {"480p": "$0.11", "1080p": "$0.55"}}},
        {"id": "nano-banana-pro", "kind": "image", "price": {"unit": "per_image", "usd": "$0.04"}},
    ]
    run(monkeypatch, lambda req: httpx.Response(200, json={"data": models}), ["models", "seedance"])
    out = capsys.readouterr().out
    assert "480p $0.11, 1080p $0.55/s" in out and "nano-banana-pro" not in out
