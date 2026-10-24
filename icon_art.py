"""Рисует иконку приложения (нужна только библиотека Pillow)."""
from PIL import Image, ImageDraw


def make_icon(size: int = 256) -> Image.Image:
    s = 4  # рисуем в 4-кратном размере и уменьшаем - так края получаются гладкими
    big = size * s
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (30, 136, 229, 255)
    white = (255, 255, 255, 255)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=bg)

    cx, cy = big / 2, big * 0.52
    # две дуги «звуковых волн»
    for r, w in ((0.30, 0.06), (0.18, 0.06)):
        R = big * r
        d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=white, width=int(big * w))
    # закрываем нижнюю часть колец цветом фона
    d.rectangle([0, cy + big * 0.02, big, big], fill=bg)
    # треугольник «AirPlay»
    d.polygon([(cx, big * 0.50), (cx - big * 0.22, big * 0.80), (cx + big * 0.22, big * 0.80)], fill=white)
    # скругляем углы заново, чтобы закрашенный прямоугольник их не испортил
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=255)
    img.putalpha(mask)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    make_icon(256).save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    make_icon(256).save("icon_preview.png")
    print("icon.ico создан")
