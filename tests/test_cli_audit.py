"""CLI tests for the 2026-10-31 API audit follow-ups, on an httpx MockTransport; nothing reaches the API."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

import revana.cli as cli
from revana import Revana

BASE = "https://api.test"
NOT_FOUND = {"error": {"code": "not_found", "message": "Not found."}}


def gen(id: str, status: str = "succeeded", **kw: Any) -> Dict[str, Any]:
    g: Dict[str, Any] = {"id": id, "object": "generation", "model": "seedance-2.5", "status": status, "output": None,
                         "quoted_usd": 1.05, "cost_usd": None, "attempts": 1, "error": None,
                         "created_at": "2026-10-31T10:00:00Z", "completed_at": None}
    g.update(kw)
    return g


# --- CLI ---


def run_cli(monkeypatch: pytest.MonkeyPatch, handler: Any, argv: List[str]) -> None:
    real = Revana

    def make() -> Revana:
        return real(api_key="k", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(cli, "Revana", make)
    cli.main(argv)


MODELS = {"data": [
    {"id": "suno-v6", "object": "model", "kind": "audio", "price": {"unit": "per_job", "usd": 0.08}},
    {"id": "nano-banana-pro", "object": "model", "kind": "image",
     "price": {"unit": "per_image", "usd": {"1K": 0.04, "2K": 0.04, "4K": 0.08}}},
    {"id": "claude-x", "object": "model", "kind": "text", "price": {"unit": "per_million_tokens", "input": 1, "output": 5}},
]}


def test_cli_models_prints_numeric_and_token_prices(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    run_cli(monkeypatch, lambda req: httpx.Response(200, json=MODELS), ["models"])
    out = capsys.readouterr().out
    assert "$0.08/job" in out
    assert "1K $0.04, 2K $0.04, 4K $0.08/image" in out
    assert "in $1/M, out $5/M tokens" in out


def test_cli_download_names_files_by_kind(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.chdir(tmp_path)
    song = gen("gen_song", model="suno-v6", output={"urls": ["https://f/a", "https://f/b"], "durations_s": [120, 118]})
    pic = gen("gen_pic", model="unlisted", output={"urls": ["https://f/c"], "width": 1024, "height": 1024})

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v1/models":
            return httpx.Response(200, json=MODELS)
        if p == "/v1/generations/gen_song":
            return httpx.Response(200, json=song)
        if p == "/v1/generations/gen_pic":
            return httpx.Response(200, json=pic)
        return httpx.Response(200, content=b"bytes")

    run_cli(monkeypatch, handler, ["download", "gen_song"])
    run_cli(monkeypatch, handler, ["download", "gen_pic"])
    assert sorted(f.name for f in tmp_path.iterdir()) == ["gen_pic.png", "gen_song-2.mp3", "gen_song.mp3"]


def test_cli_quote_and_list(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    q = {"object": "quote", "model": "seedance-2.5", "currency": "usd", "quoted_usd": 1.25, "reserved_usd": 1.4,
         "estimated_seconds": 90}
    seen: List[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v1/generations/quote":
            return httpx.Response(200, json=q)
        return httpx.Response(200, json={"object": "list", "data": [gen("gen_a", cost_usd=1.05)], "has_more": False,
                                         "next_before": None})

    run_cli(monkeypatch, handler, ["quote", "seedance-2.5", "a", "boat", "--duration", "5"])
    assert capsys.readouterr().out.strip() == "$1.25 (at most $1.4)"
    assert json.loads(seen[0].content)["input"] == {"prompt": "a boat", "duration": 5}
    run_cli(monkeypatch, handler, ["list", "--status", "succeeded", "--limit", "5"])
    assert "gen_a  succeeded" in capsys.readouterr().out
    assert dict(seen[1].url.params) == {"limit": "5", "status": "succeeded"}


def test_cli_quote_on_an_older_server(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    with pytest.raises(SystemExit) as ei:
        run_cli(monkeypatch, lambda req: httpx.Response(404, json=NOT_FOUND), ["quote", "seedance-2.5", "x"])
    assert ei.value.code == 1
    assert "deployed after 2026-10-31" in capsys.readouterr().err
