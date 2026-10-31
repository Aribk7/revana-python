"""revana CLI: `revana models`, `revana generate seedance-2.5 "a corgi surfing" -o corgi.mp4`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from . import GenerationFailedError, Revana, RevanaError, __version__
from ._base import MIME
from .types import Generation

HELP = f"""revana {__version__}: one API key for 200+ image, video and music models (https://revana.si)

Usage:
  revana models [search]              List models with prices (no key needed)
  revana generate <model> <prompt> [options]
  revana quote <model> <prompt> [options]   Price it first (free; same options as generate)
  revana get <generation-id>          Show a generation
  revana list [--limit n] [--status s] [--model m]   Your recent generations
  revana download <generation-id> [-o file]
  revana upload <file>                Upload a local file, print a URL to use as a reference
  revana balance                      What your key can spend

Generate options:
  --duration <s>        Video length in seconds
  --resolution <r>      e.g. 720p, 1080p, 2K
  --aspect <ratio>      e.g. 16:9, 9:16, 1:1
  --audio               Generate sound (--no-audio to turn it off)
  --image <url|file>    Start frame / picture to edit (local files are uploaded)
  --end-image <url|file>
  --ref <url|file>      Reference picture (repeatable)
  --video <url|file>    Clip to transform
  --set key=value       Any other input field (repeatable, JSON values allowed)
  --max-cost <usd>      Never charge more than this
  -o, --out <file>      Save the result here (default: <id>.<ext>)
  --no-wait             Submit and print the id without waiting
  --json                Print the generation as JSON

quote and list need an API version deployed after 2026-10-31.

Set REVANA_API_KEY first. Create a key at https://revana.si/dashboard"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise SystemExit(f"{message}. Run: revana --help")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="revana", add_help=False)
    p.add_argument("command", nargs="?")
    p.add_argument("args", nargs="*")
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--version", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("-o", "--out")
    p.add_argument("--duration", type=float)
    p.add_argument("--resolution")
    p.add_argument("--aspect")
    p.add_argument("--audio", action="store_true", default=None)
    p.add_argument("--no-audio", dest="audio", action="store_false")
    p.add_argument("--image")
    p.add_argument("--end-image")
    p.add_argument("--ref", action="append", default=[])
    p.add_argument("--video")
    p.add_argument("--set", action="append", default=[])
    p.add_argument("--max-cost", type=float)
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--status", choices=["queued", "running", "succeeded", "failed"])
    p.add_argument("--model")
    p.add_argument("--before")
    return p


def _media(client: Revana, ref: str) -> str:
    """A URL as is; a local file uploaded first."""
    if ref.startswith(("http://", "https://")):
        return ref
    ctype = MIME.get(os.path.splitext(ref)[1].lower())
    if not ctype:
        raise ValueError(f"Unknown file type: {ref}")
    if not os.path.isfile(ref):
        raise ValueError(f"No such file: {ref}")
    up = client.uploads.upload(ref, ctype)
    print(f"uploaded {ref}", file=sys.stderr)
    return up["url"]


def _ext_for(url: Optional[str], kind: Optional[str] = None) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower() if url else ""
    if ext and len(ext) <= 5:
        return ext
    return ".png" if kind == "image" else ".mp3" if kind == "audio" else ".mp4"


def _kind(gen: Generation, model_kind: Optional[str]) -> str:
    """image, audio or video: the model's kind, else what the output carries."""
    if model_kind in ("image", "audio", "video"):
        return model_kind
    out: Dict[str, Any] = dict(gen.get("output") or {})
    if out.get("durations_s") or ("duration_s" in out and "width" not in out and "has_audio" not in out):
        return "audio"
    if "width" in out and "duration_s" not in out:
        return "image"
    return "video"


def _model_kind(client: Revana, model: Optional[str]) -> Optional[str]:
    if not model:
        return None
    try:
        found = client.models.get(model)
    except RevanaError:
        return None
    return found.get("kind") if found else None


def _usd(v: Any) -> str:
    if isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)):
        return f"${v:g}"
    if isinstance(v, str):
        return v if v.startswith("$") else f"${v}"
    return ""


def _price(p: Any) -> str:
    """A model's list price in one line: "$0.21/s", "720p $0.21, 1080p $0.52/s", "in $1/M, out $5/M tokens"."""
    if not isinstance(p, dict):
        return ""
    unit, usd = p.get("unit"), p.get("usd")
    if unit == "per_million_tokens" or (usd is None and ("input" in p or "output" in p)):
        return f"in {_usd(p.get('input'))}/M, out {_usd(p.get('output'))}/M tokens"
    if p.get("free") is True:
        return "free"
    if unit == "per_second":
        per = "/s"
    elif unit == "per_image":
        per = "/image"
    elif unit:
        per = "/" + str(unit).replace("per_", "", 1)
    else:
        per = ""
    if isinstance(usd, (int, float, str)) and not isinstance(usd, bool):
        return f"{_usd(usd)}{per}"
    if isinstance(usd, dict):
        items = list(usd.items())
        return ", ".join(f"{k} {_usd(v)}" for k, v in items[:4]) + (", ..." if len(items) > 4 else "") + per
    return ""


def _need(v: List[str], what: str) -> str:
    if not v:
        raise ValueError(f"Missing {what}. Run: revana --help")
    return v[0]


def _save_outputs(client: Revana, gen: Generation, out: Optional[str], kind: Optional[str], echo: bool) -> None:
    urls = (gen.get("output") or {"urls": []})["urls"]
    for n, url in enumerate(urls, start=1):
        path = out if n == 1 and out else f"{gen['id']}{f'-{n}' if n > 1 else ''}{_ext_for(url, kind)}"
        with open(path, "wb") as f:
            f.write(client.generations.download(gen["id"], n))
        if echo:
            print(path)


def _run(argv: List[str]) -> None:
    a = _parser().parse_intermixed_args(argv)
    cmd, pos = a.command, a.args
    if a.version or cmd in ("version", "--version"):
        print(__version__)
        return
    if not cmd or a.help or cmd == "help":
        print(HELP)
        return
    client = Revana()

    if cmd == "models":
        q = " ".join(pos).lower()
        models = [
            m
            for m in client.models.list()
            if not q or q in f"{m['id']} {m.get('name') or ''} {m.get('maker') or ''} {m.get('kind') or ''}".lower()
        ]
        if a.json:
            print(json.dumps(models, indent=2))
            return
        for m in models:
            print(f"{m['id']:<32} {(m.get('kind') or ''):<6} {_price(m.get('price'))}")
    elif cmd == "balance":
        b = client.balance()
        if a.json:
            print(json.dumps(b, indent=2))
        else:
            print(f"${b['available_usd']:.2f} available (${b['held_usd']:.2f} held)")
    elif cmd == "get":
        print(json.dumps(client.generations.get(_need(pos, "generation id")), indent=2))
    elif cmd == "download":
        gen_id = _need(pos, "generation id")
        gen = client.generations.get(gen_id)
        if not (gen.get("output") or {"urls": []})["urls"]:
            raise ValueError(f"Generation {gen_id} has no output yet (status {gen.get('status')})")
        _save_outputs(client, gen, a.out, _kind(gen, _model_kind(client, gen.get("model"))), echo=True)
    elif cmd == "upload":
        print(_media(client, _need(pos, "file")))
    elif cmd == "list":
        page = client.generations.list(limit=a.limit, status=a.status, model=a.model, before=a.before)
        if a.json:
            print(json.dumps(page, indent=2))
            return
        for g in page["data"]:
            cost = f"${g['cost_usd']}" if g.get("cost_usd") is not None else ""
            print(f"{g['id']}  {g['status']:<9} {g['model']:<24} {cost}  {g.get('created_at', '')}")
        if page.get("has_more") and page.get("next_before"):
            print(f"more: revana list --before {page['next_before']}", file=sys.stderr)
    elif cmd in ("generate", "gen", "quote"):
        model = _need(pos, "model")
        prompt = " ".join(pos[1:])
        inp: Dict[str, Any] = {}
        if prompt:
            inp["prompt"] = prompt
        if a.duration is not None:
            inp["duration"] = int(a.duration) if a.duration.is_integer() else a.duration
        if a.resolution:
            inp["resolution"] = a.resolution
        if a.aspect:
            inp["aspect_ratio"] = a.aspect
        if a.audio is not None:
            inp["audio"] = a.audio
        if a.image:
            inp["image_url"] = _media(client, a.image)
        if a.end_image:
            inp["end_image_url"] = _media(client, a.end_image)
        if a.ref:
            inp["image_urls"] = [_media(client, r) for r in a.ref]
        if a.video:
            inp["video_url"] = _media(client, a.video)
        for kv in a.set:
            k, _, v = kv.partition("=")
            try:
                inp[k] = json.loads(v)
            except ValueError:
                inp[k] = v
        if cmd == "quote":
            quote = client.generations.quote(model=model, input=inp, max_cost_usd=a.max_cost)
            if a.json:
                print(json.dumps(quote, indent=2))
                return
            reserved = quote.get("reserved_usd")
            most = f" (at most ${reserved})" if reserved is not None and reserved > quote["quoted_usd"] else ""
            credit = f"; only ${quote.get('available_usd')} available" if quote.get("enough_credit") is False else ""
            print(f"${quote['quoted_usd']}{most}{credit}")
            return
        if a.no_wait:
            g = client.generations.create(model=model, input=inp, max_cost_usd=a.max_cost)
            print(json.dumps(g, indent=2) if a.json else f"{g['id']} {g['status']} (quoted ${g.get('quoted_usd')})")
            return
        last = [""]

        def on_update(g: Generation) -> None:
            line = f"{g['id']} {g['status']}" + (f": {g['message']}" if g.get("message") else "")
            if line != last[0]:
                print(line, file=sys.stderr)
            last[0] = line

        gen = client.generate(model=model, input=inp, max_cost_usd=a.max_cost, on_update=on_update)
        if a.json:
            print(json.dumps(gen, indent=2))
        _save_outputs(client, gen, a.out, _kind(gen, _model_kind(client, model)), echo=not a.json)
        print(f"cost ${gen.get('cost_usd') or 0}", file=sys.stderr)
    else:
        raise ValueError(f'Unknown command "{cmd}". Run: revana --help')


def main(argv: Optional[List[str]] = None) -> None:
    try:
        _run(sys.argv[1:] if argv is None else argv)
    except GenerationFailedError as err:
        hint = " (retryable: running the command again tries anew)" if err.retryable else ""
        print(f"{err}{hint}. Nothing was charged.", file=sys.stderr)
        sys.exit(1)
    except (RevanaError, ValueError, OSError) as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
