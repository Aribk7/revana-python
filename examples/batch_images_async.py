"""Generate several pictures at once with the async client.

    python examples/batch_images_async.py
"""

import asyncio

from revana import AsyncRevana, GenerationFailedError

PROMPTS = [
    "a lighthouse in thick fog, film photo",
    "a neon-lit ramen shop at night, rain",
    "a red fox in fresh snow, studio light",
    "a minimalist perfume bottle on sand",
]


async def one(revana: AsyncRevana, i: int, prompt: str) -> None:
    try:
        img = await revana.generate(model="nano-banana-pro", input={"prompt": prompt, "aspect_ratio": "1:1"})
    except GenerationFailedError as e:
        print(f"#{i} failed ({e.code}), not charged")
        return
    data = await revana.generations.download(img["id"])
    with open(f"image-{i}.png", "wb") as f:
        f.write(data)
    print(f"#{i} saved image-{i}.png (${img['cost_usd']})")


async def main() -> None:
    async with AsyncRevana() as revana:
        print(f"balance: ${(await revana.balance())['available_usd']:.2f}")
        await asyncio.gather(*(one(revana, i, p) for i, p in enumerate(PROMPTS, start=1)))


asyncio.run(main())
