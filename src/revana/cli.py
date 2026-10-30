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
  revana get <generation-id>          Show a generation
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


def _price(p: Any) -> str:
    if not isinstance(p, dict):
        return ""
    unit, usd = p.get("unit"), p.get("usd")
    if unit == "per_second":
        per = "/s"
    elif unit == "per_image":
        per = "/image"
    elif unit:
        per = "/" + str(unit).replace("per_", "", 1)
    else:
        per = ""
    if isinstance(usd, str):
        return f"{usd}{per}"
    if isinstance(usd, dict):
        return ", ".join(f"{k} {v}" for k, v in list(usd.items())[:4]) + per
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
        _save_outputs(client, gen, a.out, None, echo=True)
    elif cmd == "upload":
        print(_media(client, _need(pos, "file")))
    elif cmd in ("generate", "gen"):
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
        try:
            found = client.models.get(model)
            kind = found.get("kind") if found else None
        except Exception:
            kind = None
        _save_outputs(client, gen, a.out, kind, echo=not a.json)
        print(f"cost ${gen.get('cost_usd') or 0}", file=sys.stderr)
    else:
        raise ValueError(f'Unknown command "{cmd}". Run: revana --help')


def main(argv: Optional[List[str]] = None) -> None:
    try:
        _run(sys.argv[1:] if argv is None else argv)
    except GenerationFailedError as err:
        print(f"{err}{' (retryable)' if err.retryable else ''}. Nothing was charged.", file=sys.stderr)
        sys.exit(1)
    except (RevanaError, ValueError, OSError) as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
