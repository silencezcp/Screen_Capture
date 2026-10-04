# -*- coding: utf-8 -*-
"""生成程序图标 assets/app.ico（用 Pillow 画一个相机图标）。

    python assets/make_icon.py

想换成自己的图标时，直接用同名文件覆盖 assets/app.ico 即可。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "app.ico"
SIZE = 512
# 与界面配色一致：奶白底 + 焦糖主色
CREAM = (250, 246, 239)
CARAMEL = (224, 145, 63)
DARK = (176, 104, 36)
WHITE = (255, 255, 255)


def make_image(size: int = SIZE) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    s = size / 512.0

    # 圆角底板（奶白 + 细边框）
    draw.rounded_rectangle([16 * s, 16 * s, 496 * s, 496 * s], radius=104 * s,
                           fill=CREAM, outline=(232, 223, 210), width=max(1, int(6 * s)))
    # 相机机身
    draw.rounded_rectangle([96 * s, 176 * s, 416 * s, 400 * s], radius=44 * s, fill=CARAMEL)
    # 顶部取景器
    draw.rounded_rectangle([196 * s, 128 * s, 316 * s, 196 * s], radius=28 * s, fill=CARAMEL)
    # 镜头
    draw.ellipse([192 * s, 216 * s, 320 * s, 344 * s], fill=DARK)
    draw.ellipse([216 * s, 240 * s, 296 * s, 320 * s], fill=CREAM)
    draw.ellipse([236 * s, 252 * s, 272 * s, 288 * s], fill=CARAMEL)
    # 闪光灯
    draw.ellipse([344 * s, 208 * s, 380 * s, 244 * s], fill=CREAM)
    return image


def make_arrow(size: int = 32, color=DARK) -> Image.Image:
    """下拉框用的小三角（QSS 的 image: url() 必须是真实文件）。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    w, h = size, size
    draw.polygon([(w * 0.18, h * 0.36), (w * 0.82, h * 0.36), (w * 0.5, h * 0.72)],
                 fill=(color[0], color[1], color[2], 255))
    return image


def main() -> int:
    # 箭头是界面下拉框用的，最先保证生成
    arrow = OUT.parent / "arrow_down.png"
    make_arrow().save(arrow, format="PNG")
    print(f"已生成下拉箭头：{arrow}")

    image = make_image()
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
    for target, save in (
        (OUT, lambda: image.save(OUT, format="ICO", sizes=sizes)),
        (OUT.with_suffix(".png"),
         lambda: image.resize((256, 256), Image.Resampling.LANCZOS).save(
             OUT.with_suffix(".png"), format="PNG")),
    ):
        try:
            save()
            print(f"已生成：{target}（{target.stat().st_size} 字节）")
        except OSError as exc:
            # 程序正在运行时会占用图标文件，跳过即可
            print(f"跳过 {target}：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
