"""
theme.py

Единый источник цветов, шрифтов и отступов для всего интерфейса.

Большинство цветов заданы как кортеж ``(светлая_тема, тёмная_тема)`` — это
формат, который виджеты CustomTkinter понимают "из коробки" в параметрах
``fg_color``/``text_color``/``border_color`` и автоматически подставляют
нужное значение при переключении режима оформления (``set_appearance_mode``).

Для мест, где приходится рисовать вручную на обычном ``tkinter.Canvas``
(спарклайны, кольцевые индикаторы) или настраивать ``ttk.Style``
(``Treeview`` в диспетчере задач не умеет сам переключать тему), используйте
функцию :func:`resolve`, которая возвращает значение для *текущего*
активного режима.
"""

from typing import Tuple

import customtkinter as ctk

# ------------------------------------------------------------------ #
# Палитра. Каждое значение — (для светлой темы, для тёмной темы).
# ------------------------------------------------------------------ #
COLORS = {
    "app_bg": ("#EEF1F8", "#0E1320"),
    "sidebar_bg": ("#FFFFFF", "#131926"),
    "sidebar_border": ("#E4E8F1", "#1E2536"),
    "card_bg": ("#FFFFFF", "#1A2233"),
    "card_bg_alt": ("#F7F9FD", "#202B40"),
    "card_border": ("#E5E9F2", "#26304A"),
    "header_bg": ("#FFFFFF", "#131926"),
    "text_primary": ("#1A2233", "#EAEFFB"),
    "text_secondary": ("#69708A", "#8B93A8"),
    "text_muted": ("#9AA1B8", "#5B6478"),
    "accent": ("#3E6BE0", "#5B8DEF"),
    "accent_hover": ("#3159C4", "#7BA3F5"),
    "accent_soft": ("#E7EDFC", "#1C2A47"),
    "success": ("#0E9F6E", "#34D399"),
    "success_soft": ("#E4F8EF", "#123327"),
    "warning": ("#D97706", "#FBBF24"),
    "warning_soft": ("#FDF1DD", "#3A2C10"),
    "danger": ("#DC2626", "#F87171"),
    "danger_soft": ("#FCE8E8", "#3A1717"),
    "track": ("#E7EAF2", "#232C40"),
    "nav_hover": ("#F2F4FA", "#1B2333"),
    "divider": ("#EBEEF5", "#232C40"),
    "scrollbar": ("#D6DCE9", "#2A3348"),
}

# Явные (не режимо-зависимые) цвета обводки спарклайнов/иконок в акценте.
ACCENT_SOLID = "#5B8DEF"


def color(key: str) -> Tuple[str, str]:
    """Возвращает кортеж (светлая, тёмная) для передачи прямо в CTk-виджет."""
    return COLORS[key]


def resolve(key: str) -> str:
    """Возвращает цвет для ТЕКУЩЕГО активного режима — для ручной отрисовки
    на Canvas или настройки ``ttk.Style``, которые не умеют сами реагировать
    на переключение темы."""
    light, dark = COLORS[key]
    return dark if ctk.get_appearance_mode() == "Dark" else light


# ------------------------------------------------------------------ #
# Шрифты. Создаются лениво (CTkFont требует существующего Tk root).
# ------------------------------------------------------------------ #
_font_cache = {}


def font(name: str) -> ctk.CTkFont:
    if name not in _font_cache:
        specs = {
            "app_title": {"family": "Segoe UI", "size": 16, "weight": "bold"},
            "page_title": {"family": "Segoe UI", "size": 22, "weight": "bold"},
            "page_subtitle": {"family": "Segoe UI", "size": 13},
            "card_title": {"family": "Segoe UI", "size": 12},
            "card_value": {"family": "Segoe UI", "size": 26, "weight": "bold"},
            "card_value_small": {"family": "Segoe UI", "size": 18, "weight": "bold"},
            "card_unit": {"family": "Segoe UI", "size": 12},
            "nav_item": {"family": "Segoe UI", "size": 13},
            "body": {"family": "Segoe UI", "size": 12},
            "body_bold": {"family": "Segoe UI", "size": 12, "weight": "bold"},
            "small": {"family": "Segoe UI", "size": 11},
            "mono_small": {"family": "Consolas", "size": 11},
            "section": {"family": "Segoe UI", "size": 14, "weight": "bold"},
        }
        _font_cache[name] = ctk.CTkFont(**specs[name])
    return _font_cache[name]


# ------------------------------------------------------------------ #
# Отступы / радиусы — общие константы разметки.
# ------------------------------------------------------------------ #
PAD = 16
GAP = 14
RADIUS = 14
RADIUS_SM = 10
SIDEBAR_WIDTH = 248


def level_color(percent: float) -> str:
    """Возвращает ключ цвета COLORS в зависимости от уровня загрузки —
    используется для окраски значений ЦП/памяти/диска по порогам."""
    if percent >= 85:
        return "danger"
    if percent >= 60:
        return "warning"
    return "success"
