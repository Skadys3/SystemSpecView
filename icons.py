"""
icons.py

Программная генерация плоских линейных иконок для боковой панели навигации.

Иконки рисуются через Pillow (``ImageDraw``) с 4-кратным супersampling'ом
и затем уменьшаются с фильтром LANCZOS — это даёт гладкие сглаженные линии
без единого файла изображения в проекте и без зависимости от того, какие
именно эмодзи-шрифты установлены на компьютере пользователя.

Каждая иконка существует в двух вариантах:

* обычный (``light``/``dark``) — приглушённый нейтральный цвет, авто-
  переключаемый ``CTkImage`` в зависимости от текущей темы оформления;
* акцентный — один и тот же яркий цвет акцента в обеих темах, используется
  для подсветки выбранного пункта навигации.
"""

from typing import Callable, Dict, Tuple

from PIL import Image, ImageDraw
import customtkinter as ctk

_SUPERSAMPLE = 4

# Приглушённые нейтральные цвета обводки иконок под каждую тему.
_STROKE_LIGHT = "#4B5568"
_STROKE_DARK = "#AAB4CC"


def _canvas(size: int) -> Tuple[Image.Image, ImageDraw.ImageDraw, int]:
    hi = size * _SUPERSAMPLE
    img = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img), hi


def _finish(img: Image.Image, size: int) -> Image.Image:
    return img.resize((size, size), Image.LANCZOS)


def _stroke_width(hi: int) -> int:
    return max(3, round(hi * 0.045))


# ------------------------------------------------------------------ #
# Функции отрисовки отдельных иконок. Каждая принимает ImageDraw,
# сторону канвы в пикселях (после супersampling'а) и цвет обводки.
# ------------------------------------------------------------------ #
def _draw_dashboard(d: ImageDraw.ImageDraw, hi: int, c: str) -> None:
    w = _stroke_width(hi)
    pad = hi * 0.12
    gap = hi * 0.14
    cell = (hi - 2 * pad - gap) / 2
    for r in range(2):
        for col in range(2):
            x0 = pad + col * (cell + gap)
            y0 = pad + r * (cell + gap)
            d.rounded_rectangle([x0, y0, x0 + cell, y0 + cell], radius=cell * 0.28, outline=c, width=w)


def _draw_monitor(d: ImageDraw.ImageDraw, hi: int, c: str) -> None:
    w = _stroke_width(hi)
    x0, y0, x1, y1 = hi * 0.10, hi * 0.14, hi * 0.90, hi * 0.68
    d.rounded_rectangle([x0, y0, x1, y1], radius=hi * 0.08, outline=c, width=w)
    d.line([hi * 0.5, y1, hi * 0.5, hi * 0.82], fill=c, width=w)
    d.line([hi * 0.32, hi * 0.88, hi * 0.68, hi * 0.88], fill=c, width=w)


def _draw_cpu(d: ImageDraw.ImageDraw, hi: int, c: str) -> None:
    w = _stroke_width(hi)
    pad = hi * 0.26
    d.rounded_rectangle([pad, pad, hi - pad, hi - pad], radius=hi * 0.06, outline=c, width=w)
    inner = hi * 0.10
    d.rectangle([pad + inner, pad + inner, hi - pad - inner, hi - pad - inner], outline=c, width=max(2, w - 1))
    # "ножки" по краям
    positions = [0.30, 0.50, 0.70]
    leg = hi * 0.10
    for p in positions:
        y = hi * p
        d.line([0, y, pad, y], fill=c, width=w)
        d.line([hi - pad, y, hi, y], fill=c, width=w)
        x = hi * p
        d.line([x, 0, x, pad], fill=c, width=w)
        d.line([x, hi - pad, x, hi], fill=c, width=w)


def _draw_memory(d: ImageDraw.ImageDraw, hi: int, c: str) -> None:
    w = _stroke_width(hi)
    x0, y0, x1, y1 = hi * 0.10, hi * 0.30, hi * 0.90, hi * 0.62
    d.rounded_rectangle([x0, y0, x1, y1], radius=hi * 0.05, outline=c, width=w)
    for i in range(4):
        x = x0 + (x1 - x0) * (0.18 + i * 0.22)
        d.line([x, y1, x, y1 + hi * 0.10], fill=c, width=w)


def _draw_disk(d: ImageDraw.ImageDraw, hi: int, c: str) -> None:
    w = _stroke_width(hi)
    x0, y0, x1, y1 = hi * 0.12, hi * 0.16, hi * 0.88, hi * 0.84
    d.rounded_rectangle([x0, y0, x1, y1], radius=hi * 0.08, outline=c, width=w)
    d.line([x0, hi * 0.58, x1, hi * 0.58], fill=c, width=w)
    r = hi * 0.045
    cx, cy = x1 - hi * 0.14, hi * 0.71
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=w)


def _draw_network(d: ImageDraw.ImageDraw, hi: int, c: str) -> None:
    w = _stroke_width(hi)
    cx, cy = hi * 0.5, hi * 0.5
    r_center = hi * 0.09
    r_node = hi * 0.075
    dist = hi * 0.32
    import math

    d.ellipse([cx - r_center, cy - r_center, cx + r_center, cy + r_center], outline=c, width=w)
    for angle in (90, 210, 330):
        rad = math.radians(angle)
        nx, ny = cx + dist * math.cos(rad), cy - dist * math.sin(rad)
        ex, ey = cx + (r_center + 2) * math.cos(rad), cy - (r_center + 2) * math.sin(rad)
        sx, sy = nx - (r_node) * math.cos(rad), ny + (r_node) * math.sin(rad)
        d.line([ex, ey, sx, sy], fill=c, width=w)
        d.ellipse([nx - r_node, ny - r_node, nx + r_node, ny + r_node], outline=c, width=w)


def _draw_gpu(d: ImageDraw.ImageDraw, hi: int, c: str) -> None:
    w = _stroke_width(hi)
    x0, y0, x1, y1 = hi * 0.10, hi * 0.24, hi * 0.90, hi * 0.76
    d.rounded_rectangle([x0, y0, x1, y1], radius=hi * 0.07, outline=c, width=w)
    cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, (y1 - y0) * 0.30
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=w)
    import math

    for angle in range(0, 360, 60):
        rad = math.radians(angle)
        sx, sy = cx + r * 0.35 * math.cos(rad), cy + r * 0.35 * math.sin(rad)
        ex, ey = cx + r * 0.85 * math.cos(rad), cy + r * 0.85 * math.sin(rad)
        d.line([sx, sy, ex, ey], fill=c, width=max(2, w - 1))


def _draw_motherboard(d: ImageDraw.ImageDraw, hi: int, c: str) -> None:
    w = _stroke_width(hi)
    x0, y0, x1, y1 = hi * 0.12, hi * 0.12, hi * 0.88, hi * 0.88
    d.rounded_rectangle([x0, y0, x1, y1], radius=hi * 0.06, outline=c, width=w)
    cx0, cy0, cx1, cy1 = hi * 0.32, hi * 0.32, hi * 0.62, hi * 0.62
    d.rectangle([cx0, cy0, cx1, cy1], outline=c, width=max(2, w - 1))
    d.line([cx1, hi * 0.40, x1 - hi * 0.06, hi * 0.40], fill=c, width=max(2, w - 1))
    d.line([hi * 0.40, cy1, hi * 0.40, y1 - hi * 0.06], fill=c, width=max(2, w - 1))
    d.line([x0 + hi * 0.06, hi * 0.75, cx0, hi * 0.75], fill=c, width=max(2, w - 1))


def _draw_battery(d: ImageDraw.ImageDraw, hi: int, c: str) -> None:
    w = _stroke_width(hi)
    x0, y0, x1, y1 = hi * 0.14, hi * 0.28, hi * 0.80, hi * 0.72
    d.rounded_rectangle([x0, y0, x1, y1], radius=hi * 0.05, outline=c, width=w)
    d.rectangle([x1, hi * 0.42, x1 + hi * 0.06, hi * 0.58], fill=c)
    d.rectangle([x0 + hi * 0.08, y0 + hi * 0.08, x0 + hi * 0.24, y1 - hi * 0.08], fill=c)


def _draw_tasks(d: ImageDraw.ImageDraw, hi: int, c: str) -> None:
    w = _stroke_width(hi)
    bars = [0.35, 0.65, 0.50, 0.80]
    n = len(bars)
    slot = hi * 0.76 / n
    x0 = hi * 0.16
    base = hi * 0.86
    for i, frac in enumerate(bars):
        x = x0 + i * slot
        bw = slot * 0.55
        height = frac * hi * 0.60
        d.rounded_rectangle([x, base - height, x + bw, base], radius=bw * 0.3, outline=c, width=w)


_DRAWERS: Dict[str, Callable[[ImageDraw.ImageDraw, int, str], None]] = {
    "dashboard": _draw_dashboard,
    "monitor": _draw_monitor,
    "cpu": _draw_cpu,
    "memory": _draw_memory,
    "disk": _draw_disk,
    "network": _draw_network,
    "gpu": _draw_gpu,
    "motherboard": _draw_motherboard,
    "battery": _draw_battery,
    "tasks": _draw_tasks,
}

_cache: Dict[Tuple[str, int, str], "ctk.CTkImage"] = {}


def get_icon(name: str, size: int = 20) -> "ctk.CTkImage":
    """Возвращает нейтральную иконку, автоматически подстраивающуюся под
    текущую тему оформления (светлая/тёмная)."""
    key = (name, size, "neutral")
    if key not in _cache:
        drawer = _DRAWERS[name]
        light_img, light_draw, hi = _canvas(size)
        drawer(light_draw, hi, _STROKE_LIGHT)
        dark_img, dark_draw, _ = _canvas(size)
        drawer(dark_draw, hi, _STROKE_DARK)
        _cache[key] = ctk.CTkImage(
            light_image=_finish(light_img, size), dark_image=_finish(dark_img, size), size=(size, size)
        )
    return _cache[key]


def get_icon_accent(name: str, color: str, size: int = 20) -> "ctk.CTkImage":
    """Возвращает иконку в цвете акцента (для выбранного пункта навигации),
    одинаковую в обеих темах."""
    key = (name, size, color)
    if key not in _cache:
        drawer = _DRAWERS[name]
        img, draw, hi = _canvas(size)
        drawer(draw, hi, color)
        finished = _finish(img, size)
        _cache[key] = ctk.CTkImage(light_image=finished, dark_image=finished, size=(size, size))
    return _cache[key]
