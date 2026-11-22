#!/usr/bin/env python3
"""The README's GIFs: each frame of the window next to a drawn TV that shows what Lanterna sent to it.
compose.py FRAMES SCENE OUT.gif"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

FRAMES, SCENE, OUT = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
BG = (246, 245, 242)
W, H = 1240, 620


def film_still() -> Image.Image:
    """A made-up frame of the film: the sea at sunset."""
    w, h = 1600, 900
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h // 2):
        t = y / (h / 2)
        d.line([(0, y), (w, y)], fill=(round(250 - 60 * t), round(170 - 40 * t), round(120 + 20 * t)))
    d.ellipse([700, 300, 900, 500], fill=(255, 226, 170))
    for y in range(h // 2, h):
        t = (y - h / 2) / (h / 2)
        d.line([(0, y), (w, y)], fill=(round(60 + 40 * (1 - t)), round(90 + 30 * (1 - t)), round(130 + 20 * (1 - t))))
    for k in range(14):
        y = h // 2 + 12 + k * 26
        d.line([(800 - 90 + k * 4, y), (800 + 90 - k * 4, y)], fill=(255, 214, 160), width=4)
    return im.filter(ImageFilter.GaussianBlur(1.5))


def tv(picture: Image.Image | None) -> Image.Image:
    """A TV on its stand, the picture in its screen (or a dark screen)."""
    tw, th = 600, 338
    out = Image.new("RGBA", (tw + 24, th + 24 + 70), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    d.rounded_rectangle([0, 0, tw + 23, th + 23], radius=10, fill=(28, 28, 30))
    screen = Image.new("RGB", (tw, th), (12, 12, 14))
    if picture is not None:
        from PIL import ImageOps
        screen.paste(ImageOps.fit(picture, (tw, th), Image.LANCZOS))     # filled, as the TV shows a landscape photo
    out.paste(screen, (12, 12))
    cx = (tw + 24) // 2
    d.polygon([(cx - 20, th + 24), (cx + 20, th + 24), (cx + 34, th + 64), (cx - 34, th + 64)], fill=(46, 46, 50))
    d.rounded_rectangle([cx - 120, th + 60, cx + 120, th + 70], radius=5, fill=(46, 46, 50))
    return out


def window_card(png: Path) -> Image.Image:
    win = Image.open(png).convert("RGBA")
    win = win.resize((round(win.width * 0.92), round(win.height * 0.92)), Image.LANCZOS)
    mask = Image.new("L", win.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, win.width - 1, win.height - 1], radius=12, fill=255)
    shadow = Image.new("RGBA", (win.width + 40, win.height + 40), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([20, 24, win.width + 20, win.height + 24], radius=14, fill=(0, 0, 0, 60))
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    shadow.paste(win, (20, 20), mask)
    return shadow


def canvas(png: Path, picture: Image.Image | None) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    card = window_card(png)
    im.paste(card, (10, (H - card.height) // 2), card)
    t = tv(picture)
    im.paste(t, (W - t.width - 30, (H - t.height) // 2 + 20), t)
    return im


pictures = {p.name: Image.open(p).convert("RGB") for p in (FRAMES / "media").glob("*.jpg")}
film = film_still()
frames, durations = [], []
previous = None
for line in (FRAMES / f"{SCENE}.txt").read_text().splitlines():
    name, on_tv, hold = line.split("\t")
    picture = film if on_tv.endswith(".mkv") else pictures.get(on_tv)
    shown = canvas(FRAMES / name, picture)
    if previous is not None and picture is not None and previous[1] is not picture:
        for k in (1, 2, 3):        # the TV's picture fades from the last one
            mix = Image.blend(canvas(FRAMES / name, previous[1]) if previous[1] is not None else canvas(FRAMES / name, None),
                              shown, k / 4)
            frames.append(mix)
            durations.append(70)
    frames.append(shown)
    durations.append(round(float(hold) * 1000))
    previous = (name, picture)

small = [f.resize((W * 3 // 4, H * 3 // 4), Image.LANCZOS) for f in frames]
palette = small[len(small) // 2].quantize(colors=255, method=Image.Quantize.MEDIANCUT)
quantised = [f.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG) for f in small]
quantised[0].save(OUT, save_all=True, append_images=quantised[1:], duration=durations, loop=0, optimize=True)
small[min(len(small) - 1, 7)].save(OUT.with_suffix(".preview.png"))
print(OUT, len(frames), f"{OUT.stat().st_size // 1024} KB")
