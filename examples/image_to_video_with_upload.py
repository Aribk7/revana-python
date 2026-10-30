"""Animate a local picture: upload it, then use its URL as the start frame.

    python examples/image_to_video_with_upload.py product.png
"""

import sys

from revana import Revana

path = sys.argv[1] if len(sys.argv) > 1 else "product.png"
revana = Revana()

up = revana.uploads.upload(path)  # content type guessed from the file name; free, good for 14 days
print("uploaded:", up["url"])

video = revana.generate(
    model="seedance-2.5",
    input={
        "prompt": "the camera slowly orbits the product, soft studio light",
        "image_url": up["url"],
        "duration": 5,
        "resolution": "720p",
    },
    max_cost_usd=2,
)

with open("product.mp4", "wb") as f:
    f.write(revana.generations.download(video["id"]))
print(f"saved product.mp4, cost ${video['cost_usd']}")
