# Changelog

## Unreleased

Follow-ups from the 2026-10-31 API audit. Not published yet; the version stays 0.1.0 until a release.

### Requires an API version deployed after 2026-10-31

- `generations.quote(model=, input=, ...)`: `POST /v1/generations/quote`, a free price for the exact request
  (including what a start image adds), with `reserved_usd`, `estimated_seconds` and, for a customer key,
  `available_usd` and `enough_credit`. Retried on 429/5xx, since it changes nothing.
- `generations.list(limit=, before=, status=, model=)`: one page of `GET /v1/generations`, and
  `generations.list_all(...)`, an iterator (an async iterator on `AsyncRevana`) that follows `next_before`.
  `before` takes a string or a `datetime`.
- CLI: `revana quote` and `revana list`.
- On an older server these raise `EndpointNotAvailableError` (a `RevanaError`: status 404, code
  `endpoint_not_available`) with a message that says so.

### Works with the API before and after that deploy

- `RevanaError` has `request_id` (from `error.request_id` or `X-Request-Id`), `retry_after` (seconds, from a
  numeric or HTTP-date `Retry-After`) and `headers`. The message names the request id. All are optional: older
  servers send no request id.
- The legacy `{"error": "...", "code": ...}` shape keeps its code instead of becoming `http_error`.
- Retries never come sooner than `Retry-After`. A `Retry-After` over `max_retry_after` (new option, default 60) is
  raised instead of being cut to 60 s, and so is a 429 `concurrency_limit` without one.
- `models.list()` sends the key when the client has one, so price-book accounts see their own prices.
- `max_wait_s` is an `int` (whole seconds). `30.0` is sent as `30`; `30.5` raises `ValueError`.
- Chat streaming: `chat.completions.create(..., stream=True)` and `chat.completions.stream(...)` answer a
  `ChatStream` (`AsyncChatStream`) of chunks from server-sent events; an error event raises `RevanaError`. Before,
  a stream was parsed as JSON and failed.
- Types: `GenerationOutput.durations_s`, `Balance.price_tier` and `price_tier_until`, `Quote`, `GenerationList`.
- CLI: `revana models` prints numeric prices (`$0.08/job`) and token prices; `revana download` saves pictures as
  `.png` and music as `.mp3`, not `.mp4`.
- README: the retry rule (the same Idempotency-Key after a timeout or a 5xx; a new one after a failed generation),
  and the key prefix `sr_live_`.
