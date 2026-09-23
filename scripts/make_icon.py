"""Regenerate the Windows app icon. Requires Pillow."""

from pathlib import Path

from PIL import Image, ImageDraw


root = Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "icons"
root.mkdir(parents=True, exist_ok=True)
image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((0, 0, 255, 255), radius=42, fill="#171c20")
draw.arc((49, 42, 205, 212), start=52, end=308, fill="#d5b98f", width=27)
for x, height in [(112, 36), (133, 65), (154, 46)]:
    center = 128
    draw.rounded_rectangle((x, center - height // 2, x + 12, center + height // 2), radius=6, fill="#d5b98f")
image.save(root / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
image.resize((128, 128), Image.Resampling.LANCZOS).save(root / "128x128.png")
image.resize((32, 32), Image.Resampling.LANCZOS).save(root / "32x32.png")
