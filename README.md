# Revana Python SDK

The official Python SDK and CLI for [Revana](https://revana.si): one API key for 200+ image, video, music and chat models, including Seedance 2.5, Veo 3.1, Kling, Nano Banana Pro, GPT Image, Wan, Hailuo and MiniMax. Each request takes the cheapest working route first and falls back automatically. A failed generation is never charged.

- Sync and async clients (`Revana`, `AsyncRevana`) built on [httpx](https://www.python-httpx.org/)
- Fully typed (`py.typed`), Python 3.9+
- Automatic retries with idempotency keys, so a retry never starts a second paid generation
- A `revana` command-line tool

Prices for every model: **[revana.si/pricing](https://revana.si/pricing)** · API reference: [revana.si/openapi.json](https://revana.si/openapi.json)

## Install

```bash
pip install revana
```

Create an API key at [revana.si/dashboard](https://revana.si/dashboard) and set it:

```bash
export REVANA_API_KEY=rv_...
```

## Text to video

```python
from revana import Revana

revana = Revana()  # reads REVANA_API_KEY

video = revana.generate(
    model="seedance-2.5",
    input={"prompt": "a corgi surfing at sunset", "duration": 5, "resolution": "720p"},
    max_cost_usd=2,  # never charge more than this
)
print(video["output"]["urls"][0], video["cost_usd"])

with open("corgi.mp4", "wb") as f:
    f.write(revana.generations.download(video["id"]))
```

`generate()` submits the generation and polls until it finishes. To do it in steps:

```python
gen = revana.generations.create(model="nano-banana-pro", input={"prompt": "a red fox, studio light"})
gen = revana.generations.wait(gen["id"], timeout=600, on_update=lambda g: print(g["status"]))
```

## Async

```python
import asyncio
from revana import AsyncRevana

async def main():
    async with AsyncRevana() as revana:
        prompts = ["a lighthouse in fog", "a neon city at night", "a bowl of ramen"]
        images = await asyncio.gather(
            *(revana.generate(model="nano-banana-pro", input={"prompt": p}) for p in prompts)
        )
        for img in images:
            print(img["output"]["urls"][0])

asyncio.run(main())
```

## Upload a local file (image to video)

References are passed as URLs. For a file on your computer, `uploads.upload()` gets a link, sends the bytes and returns a URL that works for 14 days. Uploads are free; images up to 30 MB, video 50 MB, audio 15 MB.

```python
up = revana.uploads.upload("product.png")  # the content type is guessed from the name
video = revana.generate(
    model="seedance-2.5",
    input={"prompt": "the bottle spins slowly on a marble table", "image_url": up["url"], "duration": 5},
)
```

Raw bytes work too: `revana.uploads.upload(data, content_type="image/jpeg")`.

## Models and balance

```python
for m in revana.models.list():  # no key needed
    print(m["id"], m.get("kind"), m.get("price"))

print(revana.models.get("veo-3.1-fast"))
print(revana.balance()["available_usd"])
```

## Chat completions

OpenAI-compatible, on the same key:

```python
reply = revana.chat.completions.create(
    model="gpt-5-mini",
    messages=[{"role": "user", "content": "Write a 10-word ad for running shoes"}],
)
print(reply["choices"][0]["message"]["content"])
```

## Errors

```python
from revana import Revana, RevanaError, GenerationFailedError, WaitTimeoutError

try:
    video = revana.generate(model="seedance-2.5", input={"prompt": "..."}, max_cost_usd=1)
except GenerationFailedError as e:
    # The generation ran and failed. Nothing was charged.
    print(e.code, e.retryable, e.generation["id"])
except WaitTimeoutError as e:
    # Still running after `timeout` seconds; it may still finish.
    print("still waiting on", e.generation["id"])
except RevanaError as e:
    # The API refused the request, e.g. 402 insufficient_credits or 422 invalid_input.
    print(e.status, e.code, e.message)
```

| Error | When | Fields |
| --- | --- | --- |
| `RevanaError` | An HTTP error from the API, a missing key (`missing_api_key`, raised before any request), or a network failure after the retries (status 0, `network_error` / `timeout`) | `status`, `code`, `message`, `body` |
| `GenerationFailedError` | The generation ended in `failed`. Never charged. | `generation`, `code`, `retryable` |
| `WaitTimeoutError` | Waiting took longer than `timeout` | `generation` |

## Retries and idempotency

429, 5xx and network errors are retried up to `max_retries` times (default 3), honoring `Retry-After` (up to 60 s) and otherwise backing off exponentially. Only requests that are safe to repeat are retried: GETs, and POSTs that carry an `Idempotency-Key`. `generations.create()` always sends one (a random UUID unless you pass `idempotency_key=`), reused across its retries, so the same generation is never started twice. Pass your own key to make a retry safe across processes too.

## Configuration

```python
Revana(
    api_key=None,        # default: REVANA_API_KEY
    base_url=None,       # default: REVANA_BASE_URL, else https://api.revana.si
    max_retries=3,
    timeout=60,          # seconds per request
    http_client=None,    # your own httpx.Client / httpx.AsyncClient
)
```

## CLI

```bash
revana models seedance                  # list models with prices (no key needed)
revana balance
revana generate seedance-2.5 "a corgi surfing at sunset" --duration 5 --resolution 720p --max-cost 2 -o corgi.mp4
revana generate kling-2.6-pro "she turns and smiles" --image ./photo.jpg --aspect 9:16   # local files are uploaded
revana get gen_...
revana download gen_... -o out.mp4
revana upload ./reference.png
```

Generate options: `--duration`, `--resolution`, `--aspect`, `--audio`, `--image`, `--end-image`, `--ref` (repeatable), `--video`, `--set key=value` (repeatable), `--max-cost`, `-o/--out`, `--no-wait`, `--json`. Run `revana --help` for the full list.

## License

MIT
