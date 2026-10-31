"""Text to video with Seedance 2.5, saved to corgi.mp4.

    export REVANA_API_KEY=sr_live_...
    python examples/text_to_video.py
"""

from revana import GenerationFailedError, Revana

revana = Revana()

try:
    video = revana.generate(
        model="seedance-2.5",
        input={
            "prompt": "a corgi surfing a wave at sunset, slow motion, cinematic",
            "duration": 5,
            "resolution": "720p",
            "aspect_ratio": "16:9",
        },
        max_cost_usd=2,  # never charge more than this
        on_update=lambda g: print(g["status"], g.get("message", "")),
    )
except GenerationFailedError as e:
    raise SystemExit(f"Failed ({e.code}), nothing was charged: {e}")

with open("corgi.mp4", "wb") as f:
    f.write(revana.generations.download(video["id"]))
print(f"saved corgi.mp4, cost ${video['cost_usd']}")
