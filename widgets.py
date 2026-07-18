"""
widgets.py

Переиспользуемые визуальные компоненты дашборда: карточки, мини-графики
(спарклайны), кольцевые индикаторы загрузки, сетки "параметр/значение",
боковая панель навигации.

ОПТИМИЗАЦИИ:
- Sparkline: dirty-checking по хешу значений — не перерисовывает Canvas,
  если данные не изменились.
- RingGauge: dirty-checking по проценту + тексту + цвету — не перерисовывает,
  если ничего не поменялось.
- ProgressStatCard: dirty-checking по value_text + percent + subtitle.
- HeroStatCard: dirty-checking через дочерние виджеты (RingGauge и Sparkline
  сами решают, надо ли перерисовываться).
"""

from tkinter import ttk
import tkinter as tk
from typing import Callable, List, Optional, Sequence, Tuple

import customtkinter as ctk

import icons
import theme

# ------------------------------------------------------------------ #
# Реестр Canvas-виджетов для ручной смены темы
# ------------------------------------------------------------------ #
_canvas_registry: List["_ThemedCanvas"] = []


def refresh_all_canvas_themes() -> None:
    for widget in list(_canvas_registry):
        try:
            widget.apply_theme()
        except tk.TclError:
            _canvas_registry.remove(widget)


class _ThemedCanvas(tk.Canvas):
    """Базовый класс для Canvas-виджетов, следящих за темой оформления."""

    def __init__(self, master, bg_key: str = "card_bg", **kwargs):
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        super().__init__(master, bg=theme.resolve(bg_key), **kwargs)
        self._bg_key = bg_key
        _canvas_registry.append(self)

    def apply_theme(self) -> None:
        self.configure(bg=theme.resolve(self._bg_key))
        self._redraw()

    def _redraw(self) -> None:
        pass


# ------------------------------------------------------------------ #
# Спарклайн с dirty-checking
# ------------------------------------------------------------------ #
class Sparkline(_ThemedCanvas):
    __slots__ = ("_color_key", "_values", "_vmin", "_vmax", "_last_hash", "_last_ck")

    def __init__(self, master, bg_key: str = "card_bg", color_key: str = "accent", height: int = 44, **kwargs):
        super().__init__(master, bg_key=bg_key, height=height, **kwargs)
        self._color_key = color_key
        self._values: Sequence[float] = []
        self._vmin = 0.0
        self._vmax = 100.0
        self._last_hash = -1
        self._last_ck = ""
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_color_key(self, color_key: str) -> None:
        self._color_key = color_key

    def update_values(self, values: Sequence[float], vmin: float = 0.0, vmax: Optional[float] = None) -> None:
        new_hash = hash((tuple(values), vmin, vmax))
        if new_hash == self._last_hash and self._last_ck == self._color_key:
            return
        self._last_hash = new_hash
        self._last_ck = self._color_key
        self._values = list(values)
        self._vmin = vmin
        self._vmax = vmax if vmax is not None else (max(self._values) if self._values else 100.0)
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 2 or h <= 2 or len(self._values) < 2:
            return
        vmin, vmax = self._vmin, self._vmax
        if vmax <= vmin:
            vmax = vmin + 1.0
        n = len(self._values)
        step = w / (n - 1)
        pad = 3
        usable = h - 2 * pad
        points = []
        for i, v in enumerate(self._values):
            t = min(max((v - vmin) / (vmax - vmin), 0.0), 1.0)
            points.append((i * step, pad + (1 - t) * usable))

        line_color = theme.resolve(self._color_key)
        soft_key = self._color_key + "_soft"
        fill_color = theme.resolve(soft_key) if soft_key in theme.COLORS else theme.resolve("track")

        poly = [(0.0, float(h))] + points + [(float(w), float(h))]
        self.create_polygon([c for p in poly for c in p], fill=fill_color, outline="")
        self.create_line([c for p in points for c in p], fill=line_color, width=2, smooth=True, joinstyle="round")


# ------------------------------------------------------------------ #
# Кольцевой индикатор с dirty-checking
# ------------------------------------------------------------------ #
class RingGauge(_ThemedCanvas):
    __slots__ = ("_size", "_thickness", "_percent", "_center_text", "_color_key",
                 "_last_pct", "_last_ck", "_last_ct")

    def __init__(self, master, bg_key: str = "card_bg", size: int = 96, thickness: int = 10, **kwargs):
        super().__init__(master, bg_key=bg_key, width=size, height=size, **kwargs)
        self._size = size
        self._thickness = thickness
        self._percent = 0.0
        self._center_text = "—"
        self._color_key = "accent"
        self._last_pct = -1.0
        self._last_ck = ""
        self._last_ct = ""

    def update_value(self, percent: float, center_text: Optional[str] = None, color_key: Optional[str] = None) -> None:
        new_pct = max(0.0, min(100.0, percent))
        new_ct = center_text if center_text is not None else f"{percent:.0f}%"
        new_ck = color_key or self._color_key
        if (abs(new_pct - self._last_pct) < 0.1 and
            new_ct == self._last_ct and
            new_ck == self._last_ck):
            return
        self._last_pct = new_pct
        self._last_ct = new_ct
        self._last_ck = new_ck
        self._percent = new_pct
        self._center_text = new_ct
        self._color_key = new_ck
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        s, t = self._size, self._thickness
        pad = t / 2 + 2
        self.create_oval(pad, pad, s - pad, s - pad, outline=theme.resolve("track"), width=t)
        if self._percent > 0.6:
            import math

            cx = cy = s / 2
            r = s / 2 - pad
            extent_deg = (self._percent / 100.0) * 359.4
            steps = max(2, int(extent_deg / 3) + 1)
            points = []
            for i in range(steps + 1):
                ang = math.radians(90 - (extent_deg * i / steps))
                points.append((cx + r * math.cos(ang), cy - r * math.sin(ang)))
            flat = [coord for point in points for coord in point]
            self.create_line(
                *flat, fill=theme.resolve(self._color_key), width=t, capstyle="round", joinstyle="round"
            )
        self.create_text(
            s / 2, s / 2, text=self._center_text, fill=theme.resolve("text_primary"),
            font=theme.font("card_value_small"),
        )


# ------------------------------------------------------------------ #
# Базовая карточка
# ------------------------------------------------------------------ #
class Card(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        kwargs.setdefault("corner_radius", theme.RADIUS)
        kwargs.setdefault("fg_color", theme.color("card_bg"))
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", theme.color("card_border"))
        super().__init__(master, **kwargs)


def _card_header(parent, title: str, icon_name: Optional[str], row: int = 0, pad_top: int = 14) -> None:
    header = ctk.CTkFrame(parent, fg_color="transparent")
    header.grid(row=row, column=0, sticky="ew", padx=16, pady=(pad_top, 0))
    header.grid_columnconfigure(0, weight=1)
    ctk.CTkLabel(
        header, text=title, font=theme.font("card_title"), text_color=theme.color("text_secondary"), anchor="w"
    ).grid(row=0, column=0, sticky="w")
    if icon_name:
        ctk.CTkLabel(header, text="", image=icons.get_icon(icon_name, 17)).grid(row=0, column=1, sticky="e")


# ------------------------------------------------------------------ #
# Крупная карточка-метрика (dirty-checking через дочерние виджеты)
# ------------------------------------------------------------------ #
class HeroStatCard(Card):
    def __init__(self, master, title: str, icon_name: Optional[str] = None, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(0, weight=1)

        _card_header(self, title, icon_name)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=16, pady=(6, 0))
        body.grid_columnconfigure(1, weight=1)

        self._ring = RingGauge(body, bg_key="card_bg", size=84, thickness=9)
        self._ring.grid(row=0, column=0, rowspan=2, sticky="w")

        self._detail_label = ctk.CTkLabel(
            body, text="", font=theme.font("body"), text_color=theme.color("text_primary"),
            anchor="w", justify="left",
        )
        self._detail_label.grid(row=0, column=1, sticky="w", padx=(14, 0))

        self._sub_label = ctk.CTkLabel(
            body, text="", font=theme.font("small"), text_color=theme.color("text_secondary"),
            anchor="w", justify="left",
        )
        self._sub_label.grid(row=1, column=1, sticky="w", padx=(14, 0), pady=(2, 0))

        self._spark = Sparkline(self, bg_key="card_bg", color_key="accent", height=46)
        self._spark.grid(row=2, column=0, sticky="ew", padx=14, pady=(12, 14))

        self._last_detail = ""
        self._last_sub = ""

    def update(
        self,
        percent: float,
        detail: str,
        subtitle: str,
        history: Sequence[float],
        history_max: float = 100.0,
    ) -> None:
        level_key = theme.level_color(percent)
        self._ring.update_value(percent, color_key=level_key)
        if detail != self._last_detail:
            self._last_detail = detail
            self._detail_label.configure(text=detail)
        if subtitle != self._last_sub:
            self._last_sub = subtitle
            self._sub_label.configure(text=subtitle)
        self._spark.set_color_key(level_key)
        self._spark.update_values(history, vmin=0, vmax=history_max)


# ------------------------------------------------------------------ #
# Карточка с прогресс-баром и dirty-checking
# ------------------------------------------------------------------ #
class ProgressStatCard(Card):
    __slots__ = ("_value_label", "_percent_label", "_bar", "_sub_label",
                 "_last_vt", "_last_pct", "_last_sub")

    def __init__(self, master, title: str, icon_name: Optional[str] = None, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(0, weight=1)

        _card_header(self, title, icon_name)

        value_row = ctk.CTkFrame(self, fg_color="transparent")
        value_row.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 0))
        value_row.grid_columnconfigure(0, weight=1)

        self._value_label = ctk.CTkLabel(
            value_row, text="—", font=theme.font("card_value_small"), text_color=theme.color("text_primary"),
            anchor="w",
        )
        self._value_label.grid(row=0, column=0, sticky="w")
        self._percent_label = ctk.CTkLabel(
            value_row, text="", font=theme.font("body_bold"), text_color=theme.color("text_secondary"), anchor="e"
        )
        self._percent_label.grid(row=0, column=1, sticky="e")

        self._bar = ctk.CTkProgressBar(
            self, height=8, corner_radius=4, fg_color=theme.color("track"), progress_color=theme.color("accent")
        )
        self._bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 6))
        self._bar.set(0)

        self._sub_label = ctk.CTkLabel(
            self, text="", font=theme.font("small"), text_color=theme.color("text_secondary"), anchor="w"
        )
        self._sub_label.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 14))

        self._last_vt = ""
        self._last_pct = -999.0
        self._last_sub = ""

    def update(self, value_text: str, percent: float, subtitle: str = "", muted: bool = False) -> None:
        pct_rounded = round(percent, 1)
        if value_text == self._last_vt and pct_rounded == self._last_pct and subtitle == self._last_sub:
            return
        self._last_vt = value_text
        self._last_pct = pct_rounded
        self._last_sub = subtitle

        self._value_label.configure(text=value_text)
        self._percent_label.configure(text="" if muted else f"{percent:.0f}%")
        self._bar.set(0.0 if muted else max(0.0, min(1.0, percent / 100.0)))
        self._bar.configure(progress_color=theme.color("track" if muted else theme.level_color(percent)))
        self._sub_label.configure(text=subtitle)


# ------------------------------------------------------------------ #
# Простая карточка "значение без графика"
# ------------------------------------------------------------------ #
class FactCard(Card):
    __slots__ = ("_value_label", "_sub_label", "_last_vt", "_last_sub")

    def __init__(self, master, title: str, icon_name: Optional[str] = None, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(0, weight=1)
        _card_header(self, title, icon_name)
        self._value_label = ctk.CTkLabel(
            self, text="—", font=theme.font("card_value_small"), text_color=theme.color("text_primary"),
            anchor="w", justify="left", wraplength=220,
        )
        self._value_label.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 2))
        self._sub_label = ctk.CTkLabel(
            self, text="", font=theme.font("small"), text_color=theme.color("text_secondary"),
            anchor="w", justify="left", wraplength=220,
        )
        self._sub_label.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 14))
        self._last_vt = ""
        self._last_sub = ""

    def update(self, value_text: str, subtitle: str = "") -> None:
        if value_text == self._last_vt and subtitle == self._last_sub:
            return
        self._last_vt = value_text
        self._last_sub = subtitle
        self._value_label.configure(text=value_text)
        self._sub_label.configure(text=subtitle)


# ------------------------------------------------------------------ #
# Сетка "параметр -> значение"
# ------------------------------------------------------------------ #
class InfoGrid(Card):
    def __init__(self, master, title: Optional[str] = None, icon_name: Optional[str] = None, columns: int = 1, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self._columns = columns
        row = 0
        if title:
            _card_header(self, title, icon_name, row=0, pad_top=16)
            row = 1
        self._body = ctk.CTkFrame(self, fg_color="transparent")
        self._body.grid(row=row, column=0, sticky="ew", padx=16, pady=(8 if title else 16, 14))
        for c in range(columns):
            self._body.grid_columnconfigure(c, weight=1, uniform="infogrid")
        self._rows: List[Tuple[ctk.CTkLabel, ctk.CTkLabel]] = []

    def set_rows(self, rows: Sequence[Tuple[str, str]]) -> None:
        needed = len(rows)
        while len(self._rows) < needed:
            idx = len(self._rows)
            col = idx % self._columns
            line = idx // self._columns
            cell = ctk.CTkFrame(self._body, fg_color="transparent")
            cell.grid(row=line, column=col, sticky="ew", padx=(0, 18 if self._columns > 1 else 0), pady=4)
            cell.grid_columnconfigure(0, weight=0)
            cell.grid_columnconfigure(1, weight=1)
            label = ctk.CTkLabel(
                cell, text="", font=theme.font("small"), text_color=theme.color("text_secondary"), anchor="w"
            )
            label.grid(row=0, column=0, sticky="w")
            value = ctk.CTkLabel(
                cell, text="", font=theme.font("body_bold"), text_color=theme.color("text_primary"),
                anchor="e", justify="right",
            )
            value.grid(row=1, column=0, sticky="w", pady=(1, 0))
            self._rows.append((label, value))

        for i, (key, val) in enumerate(rows):
            label, value = self._rows[i]
            label.configure(text=key)
            value.configure(text=str(val))
        for i in range(needed, len(self._rows)):
            label, value = self._rows[i]
            label.configure(text="")
            value.configure(text="")


# ------------------------------------------------------------------ #
# Заголовок страницы
# ------------------------------------------------------------------ #
class PageHeader(ctk.CTkFrame):
    def __init__(self, master, title: str, subtitle: str = "", **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self, text=title, font=theme.font("page_title"), text_color=theme.color("text_primary"), anchor="w"
        ).grid(row=0, column=0, sticky="w")
        self._subtitle = ctk.CTkLabel(
            self, text=subtitle, font=theme.font("page_subtitle"), text_color=theme.color("text_secondary"),
            anchor="w",
        )
        self._subtitle.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.actions = ctk.CTkFrame(self, fg_color="transparent", height=1)
        self.actions.grid(row=0, column=1, rowspan=2, sticky="e")

    def set_subtitle(self, text: str) -> None:
        self._subtitle.configure(text=text)


# ------------------------------------------------------------------ #
# Боковая панель навигации
# ------------------------------------------------------------------ #
class Sidebar(ctk.CTkFrame):
    def __init__(self, master, items: Sequence[Tuple[str, str, str]], on_select: Callable[[str], None], **kwargs):
        kwargs.setdefault("fg_color", theme.color("sidebar_bg"))
        kwargs.setdefault("corner_radius", 0)
        kwargs.setdefault("width", theme.SIDEBAR_WIDTH)
        super().__init__(master, **kwargs)
        self.grid_propagate(False)
        self._on_select = on_select
        self._buttons: dict = {}
        self._selected: Optional[str] = None
        self._icon_map: dict = {}

        border = ctk.CTkFrame(self, width=1, fg_color=theme.color("sidebar_border"))
        border.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")

        brand = ctk.CTkFrame(self, fg_color="transparent")
        brand.pack(fill="x", padx=20, pady=(24, 18))
        ctk.CTkLabel(brand, text="", image=icons.get_icon_accent("dashboard", theme.ACCENT_SOLID, 24)).pack(
            side="left"
        )
        ctk.CTkLabel(
            brand, text="Характеристики ПК", font=theme.font("app_title"), text_color=theme.color("text_primary"),
            anchor="w", justify="left", wraplength=170,
        ).pack(side="left", padx=(10, 0))

        nav_scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", scrollbar_button_color=theme.color("scrollbar"))
        nav_scroll.pack(fill="both", expand=True, padx=10)

        for key, label, icon_name in items:
            self._icon_map[key] = icon_name
            btn = self._make_nav_button(nav_scroll, key, label, icon_name)
            btn.pack(fill="x", pady=2)
            self._buttons[key] = btn

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=16, pady=(10, 18), side="bottom")
        self.bottom_area = bottom

    def _make_nav_button(self, parent, key: str, label: str, icon_name: str) -> ctk.CTkButton:
        btn = ctk.CTkButton(
            parent,
            text=f"  {label}",
            image=icons.get_icon(icon_name, 18),
            anchor="w",
            font=theme.font("nav_item"),
            fg_color="transparent",
            text_color=theme.color("text_primary"),
            hover_color=theme.color("nav_hover"),
            corner_radius=theme.RADIUS_SM,
            height=38,
            command=lambda: self._on_select(key),
        )
        return btn

    def set_selected(self, key: str) -> None:
        if self._selected == key:
            return
        if self._selected in self._buttons:
            self._buttons[self._selected].configure(
                fg_color="transparent", text_color=theme.color("text_primary"),
                image=icons.get_icon(self._icon_of(self._selected), 18),
            )
        self._selected = key
        btn = self._buttons[key]
        btn.configure(
            fg_color=theme.color("accent_soft"), text_color=theme.color("accent"),
            image=icons.get_icon_accent(self._icon_of(key), theme.ACCENT_SOLID, 18),
        )

    def register_icon(self, key: str, icon_name: str) -> None:
        self._icon_map[key] = icon_name

    def _icon_of(self, key: Optional[str]) -> str:
        return self._icon_map.get(key, "dashboard") if key else "dashboard"


# ------------------------------------------------------------------ #
# Стилизация Treeview
# ------------------------------------------------------------------ #
def style_treeview(style: ttk.Style) -> None:
    bg = theme.resolve("card_bg")
    alt = theme.resolve("card_bg_alt")
    fg = theme.resolve("text_primary")
    heading_bg = theme.resolve("card_bg_alt")
    selected = theme.resolve("accent_soft")
    accent = theme.resolve("accent")

    style.configure(
        "Treeview", background=bg, fieldbackground=bg, foreground=fg, rowheight=26,
        font=("Segoe UI", 10), borderwidth=0,
    )
    style.map(
        "Treeview",
        background=[("selected", selected)],
        foreground=[("selected", accent)],
    )
    style.configure(
        "Treeview.Heading", background=heading_bg, foreground=theme.resolve("text_secondary"),
        font=("Segoe UI", 10, "bold"), borderwidth=0, relief="flat",
    )
    style.map("Treeview.Heading", background=[("active", heading_bg)])

    style.configure(
        "Vertical.TScrollbar", background=theme.resolve("scrollbar"), troughcolor=bg, borderwidth=0, arrowsize=12
    )
    style.configure(
        "Horizontal.TScrollbar", background=theme.resolve("scrollbar"), troughcolor=bg, borderwidth=0, arrowsize=12
    )
