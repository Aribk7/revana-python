"""Price first with the server's free quote (API versions deployed after 2026-10-31), falling back to the list
price on an older server; then generate capped at that price. A retryable failure is tried once more, which makes
a new Idempotency-Key (the old one keeps answering the failed generation).

  REVANA_API_KEY=... python examples/quote_then_generate.py "a paper boat in the rain"
"""

import math
import sys

from revana import EndpointNotAvailableError, GenerationFailedError, Revana

revana = Revana()
model = "seedance-2.5"
inp = {"prompt": sys.argv[1] if len(sys.argv) > 1 else "a paper boat in the rain", "duration": 5, "resolution": "720p"}

try:
    price = revana.generations.quote(model=model, input=inp)["quoted_usd"]
except EndpointNotAvailableError:
    listed = revana.models.get(model)  # an older server: price from the list
    assert listed is not None
    price = math.ceil(round(listed["price"]["usd"]["720p"] * 5 * 100, 6) - 1e-9) / 100
print(f"quote: ${price}")

for attempt in (1, 2):
    try:
        video = revana.generate(model=model, input=inp, max_cost_usd=price)
        break
    except GenerationFailedError as e:
        if not e.retryable or attempt == 2:
            raise SystemExit(f"Failed ({e.code}), nothing was charged: {e}") from None
        print(f"{e}; trying once more (failed generations cost nothing)", file=sys.stderr)

with open("video.mp4", "wb") as f:
    f.write(revana.generations.download(video["id"]))
print(f"saved video.mp4, cost ${video['cost_usd']}")
