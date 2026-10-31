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
export REVANA_API_KEY=sr_live_...
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

## Price it first

```python
from revana import EndpointNotAvailableError

quote = revana.generations.quote(model="seedance-2.5", input={"prompt": "a corgi surfing", "duration": 5})
print(quote["quoted_usd"], quote.get("enough_credit"))  # free: nothing is created or held
video = revana.generate(model="seedance-2.5", input={"prompt": "a corgi surfing", "duration": 5},
                        max_cost_usd=quote["quoted_usd"])
```

`generations.quote()` takes the same arguments as `create()` and prices the exact request the way the create will, including what a start image adds on some models, which `models.list()` does not show. **It requires an API version deployed after 2026-10-31**; an older server raises `EndpointNotAvailableError` (a `RevanaError` with status 404 and code `endpoint_not_available`), and then you price from `models.list()`.

## Your generations

```python
page = revana.generations.list(limit=20, status="succeeded")  # {"data", "has_more", "next_before"}
for g in revana.generations.list_all(model="seedance-2.5"):   # every page, newest first
    print(g["id"], g["cost_usd"])
```

Pass a page's `next_before` as `before` for the next one, or let `list_all()` do it (`async for` on `AsyncRevana`). **It requires an API version deployed after 2026-10-31**; an older server raises `EndpointNotAvailableError`.

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
for m in revana.models.list():  # no key needed; with one, your account's own prices
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

Streamed, as server-sent events:

```python
with revana.chat.completions.create(model="gpt-5-mini", messages=messages, stream=True) as stream:
    for chunk in stream:
        if chunk["choices"]:
            print(chunk["choices"][0]["delta"].get("content", ""), end="", flush=True)
```

`chat.completions.stream(...)` does the same; on `AsyncRevana`, `await` it and use `async for`. Chat is billed per token and is never retried automatically.

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
    # The API refused the request, e.g. 402 insufficient_credits, 422 invalid_input, 403 card_required.
    print(e.status, e.code, e.message, e.request_id)  # quote request_id when asking for help
```

| Error | When | Fields |
| --- | --- | --- |
| `RevanaError` | An HTTP error from the API, a missing key (`missing_api_key`, raised before any request), or a network failure after the retries (status 0, `network_error` / `timeout`) | `status`, `code`, `message`, `body`, `headers`, `request_id`, `retry_after` |
| `EndpointNotAvailableError` | A `RevanaError`: `quote()` or `list()` on an API server from before the 2026-10-31 deploy | as `RevanaError` |
| `GenerationFailedError` | The generation ended in `failed`. Never charged. | `generation`, `code`, `retryable` |
| `WaitTimeoutError` | Waiting took longer than `timeout` | `generation` |

`request_id` (from `error.request_id` or the `X-Request-Id` header) and `retry_after` (seconds, from `Retry-After` on 429 and 503) are sent by API versions deployed after 2026-10-31; on an older server they are `None`. The legacy `{"error": "...", "code": ...}` shape is read too.

## Retries and idempotency

- 429, 5xx and network errors are retried up to `max_retries` times (default 3), never sooner than `Retry-After`, and otherwise with exponential backoff. A `Retry-After` longer than `max_retry_after` (60 s), or a 429 `concurrency_limit` without one, is raised to you instead: a slot frees only when one of your generations finishes.
- Only requests that are safe to repeat are retried: GETs, quotes, and POSTs that carry an `Idempotency-Key`. `generations.create()` always sends one (a random UUID unless you pass `idempotency_key=`), reused across its retries, so the same generation is never started twice. To resend a create yourself after a timeout or a 5xx, pass the same `idempotency_key=`.
- After a generation has **failed** (`GenerationFailedError`), call again **without** the old key: the old key keeps answering the failed generation. Each `generate()` call makes a new key.

## Configuration

```python
Revana(
    api_key=None,        # default: REVANA_API_KEY
    base_url=None,       # default: REVANA_BASE_URL, else https://api.revana.si
    max_retries=3,
    timeout=60,          # seconds per request (httpx: per connect, read and write)
    max_retry_after=60,  # a longer Retry-After is raised, not waited out
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
revana quote seedance-2.5 "slow orbit" --image ./product.png --duration 5   # free
revana list --status succeeded --limit 10
```

`quote` and `list` require an API version deployed after 2026-10-31. `download` names files by the model's kind (`.png`, `.mp3`, `.mp4`).

Generate options: `--duration`, `--resolution`, `--aspect`, `--audio`, `--image`, `--end-image`, `--ref` (repeatable), `--video`, `--set key=value` (repeatable), `--max-cost`, `-o/--out`, `--no-wait`, `--json`. Run `revana --help` for the full list.

## License

MIT
