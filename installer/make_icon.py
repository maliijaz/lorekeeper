"""Draws the Lorekeeper icon -> assets/icon.ico (multi-size) and static/icon.png."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
S = 1024


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw() -> Image.Image:
    # Background: rounded tile with a diagonal leather-brown gradient.
    grad = Image.new("RGB", (S, S))
    top, bottom = (150, 96, 52), (72, 40, 20)
    px = grad.load()
    for y in range(S):
        for x in range(S):
            px[x, y] = lerp(top, bottom, min(1, (x * 0.35 + y) / (S * 1.35)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=220, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    # Soft shadow under the book.
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.polygon([(160, 720), (512, 790), (864, 720), (864, 800), (512, 870), (160, 800)], fill=(0, 0, 0, 110))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(28)))

    d = ImageDraw.Draw(img)
    cream, edge, spine = (250, 238, 214, 255), (214, 190, 150, 255), (120, 72, 36, 255)
    # Left and right pages (slightly curved via polygons).
    left = [(150, 330), (300, 300), (430, 312), (512, 360), (512, 800), (430, 756), (300, 744), (150, 770)]
    right = [(1024 - x, y) for x, y in left]
    d.polygon(left, fill=cream, outline=edge)
    d.polygon(right, fill=cream, outline=edge)
    # Page thickness at the bottom.
    d.polygon([(150, 770), (300, 744), (430, 756), (512, 800), (512, 822), (430, 780), (300, 770), (150, 796)], fill=edge)
    d.polygon([(874, 770), (724, 744), (594, 756), (512, 800), (512, 822), (594, 780), (724, 770), (874, 796)], fill=edge)
    d.line([(512, 360), (512, 812)], fill=spine, width=10)
    # Text lines on the pages.
    for i in range(6):
        y = 400 + i * 58
        d.line([(210, y + 8 - i * 2), (450, y + 30 - i * 2)], fill=(196, 170, 130, 255), width=12)
        d.line([(574, y + 30 - i * 2), (814, y + 8 - i * 2)], fill=(196, 170, 130, 255), width=12)

    # Four-point sparkle above the book: the "smart" part.
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([412, 90, 612, 290], fill=(255, 214, 120, 150))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(40)))
    d = ImageDraw.Draw(img)
    cx, cy, r, w = 512, 190, 120, 34
    d.polygon([(cx, cy - r), (cx + w, cy - w), (cx + r, cy), (cx + w, cy + w),
               (cx, cy + r), (cx - w, cy + w), (cx - r, cy), (cx - w, cy - w)], fill=(255, 226, 150, 255))
    d.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], fill=(255, 250, 230, 255))
    return img


if __name__ == "__main__":
    img = draw()
    (ROOT / "assets").mkdir(exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(ROOT / "static" / "icon.png")
    img.save(ROOT / "assets" / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icon written")
