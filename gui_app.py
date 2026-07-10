"""
gui_app.py

Графический интерфейс приложения «Просмотр характеристик компьютера»
на основе CustomTkinter: боковая панель навигации, дашборд с карточками,
спарклайнами и кольцевыми индикаторами загрузки, детальные страницы по
каждому виду оборудования и диспетчер задач. Поддерживает тёмную и светлую
тему с переключателем.

Сбор данных выполняется в фоновом потоке (см. ``collectors``), чтобы более
медленные операции (запросы WMI, замер загрузки CPU) не «замораживали»
интерфейс; результаты передаются обратно в главный поток через
потокобезопасную очередь.
"""

import collections
import json
import logging
import queue
import threading
import tkinter as tk
from dataclasses import asdict
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

import customtkinter as ctk

import icons
import theme
import widgets
from collectors import SystemInfoCollector
from models import ProcessDetails, ProcessInfo, SystemSnapshot

logger = logging.getLogger(__name__)

APP_TITLE = "Просмотр характеристик компьютера"
APP_MIN_WIDTH = 1080
APP_MIN_HEIGHT = 700
HISTORY_LENGTH = 60

# Столбцы вкладки «Диспетчер задач»: внутренний ключ -> (заголовок, ширина)
PROCESS_COLUMNS: Tuple[Tuple[str, str, int], ...] = (
    ("pid", "PID", 60),
    ("name", "Имя процесса", 220),
    ("user", "Пользователь", 120),
    ("status", "Состояние", 110),
    ("cpu", "ЦП, %", 70),
    ("mem_mb", "Память, МБ", 100),
    ("mem_pct", "Память, %", 80),
    ("disk_read", "Диск чтение, КБ/с", 130),
    ("disk_write", "Диск запись, КБ/с", 130),
    ("net", "Сеть, подкл.", 90),
    ("threads", "Потоки", 70),
)

_PROCESS_SORT_KEYS = {
    "pid": lambda p: p.pid,
    "name": lambda p: p.name.lower(),
    "user": lambda p: p.username.lower(),
    "status": lambda p: p.status,
    "cpu": lambda p: p.cpu_percent,
    "mem_mb": lambda p: p.memory_mb,
    "mem_pct": lambda p: p.memory_percent,
    "disk_read": lambda p: p.disk_read_kb_s,
    "disk_write": lambda p: p.disk_write_kb_s,
    "net": lambda p: p.network_connections,
    "threads": lambda p: p.num_threads,
}
_PROCESS_DESCENDING_BY_DEFAULT = {"cpu", "mem_mb", "mem_pct", "disk_read", "disk_write", "net", "threads"}

_NAV_ITEMS: Tuple[Tuple[str, str, str], ...] = (
    ("dashboard", "Дашборд", "dashboard"),
    ("os", "Система", "monitor"),
    ("cpu", "Процессор", "cpu"),
    ("memory", "Память", "memory"),
    ("disk", "Диски", "disk"),
    ("network", "Сеть", "network"),
    ("gpu", "Видеокарта", "gpu"),
    ("motherboard", "Материнская плата", "motherboard"),
    ("battery", "Батарея", "battery"),
    ("processes", "Диспетчер задач", "tasks"),
)


class CoreBar(ctk.CTkFrame):
    """Компактная строка загрузки одного логического ядра ЦП (без рамки
    карточки — используются десятками внутри одной карточки "Ядра")."""

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        self._name_label = ctk.CTkLabel(
            top, text="", font=theme.font("small"), text_color=theme.color("text_secondary"), anchor="w"
        )
        self._name_label.grid(row=0, column=0, sticky="w")
        self._pct_label = ctk.CTkLabel(
            top, text="", font=theme.font("small"), text_color=theme.color("text_primary"), anchor="e"
        )
        self._pct_label.grid(row=0, column=1, sticky="e")
        self._bar = ctk.CTkProgressBar(
            self, height=6, corner_radius=3, fg_color=theme.color("track"), progress_color=theme.color("accent")
        )
        self._bar.grid(row=1, column=0, sticky="ew", pady=(3, 8))
        self._bar.set(0)

    def update_core(self, index: int, percent: float) -> None:
        self._name_label.configure(text=f"Ядро {index}")
        self._pct_label.configure(text=f"{percent:.0f}%")
        self._bar.set(max(0.0, min(1.0, percent / 100.0)))
        self._bar.configure(progress_color=theme.color(theme.level_color(percent)))


class SystemInfoApp(ctk.CTk):
    """Главное окно приложения «Просмотр характеристик компьютера»."""

    def __init__(self) -> None:
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        super().__init__()

        self.title(APP_TITLE)
        self.minsize(APP_MIN_WIDTH, APP_MIN_HEIGHT)
        self.geometry("1280x800")
        self.configure(fg_color=theme.color("app_bg"))

        self._collector = SystemInfoCollector()
        self._result_queue: "queue.Queue[SystemSnapshot]" = queue.Queue()
        self._latest_snapshot: Optional[SystemSnapshot] = None
        self._live_updates_var = tk.BooleanVar(value=True)
        self._worker_started = False
        self._is_first_run = True

        # История значений для спарклайнов (общая загрузка ЦП и памяти).
        self._cpu_history: "collections.deque[float]" = collections.deque(maxlen=HISTORY_LENGTH)
        self._mem_history: "collections.deque[float]" = collections.deque(maxlen=HISTORY_LENGTH)
        self._smoothed_cpu_cores: Optional[List[float]] = None
        self._last_cpu_total: float = 0.0
        self._last_cpu_cores: List[float] = []

        # Динамические списки карточек (диски/сеть/видеокарты меняются по
        # количеству в зависимости от оборудования пользователя).
        self._disk_cards: List[widgets.ProgressStatCard] = []
        self._network_cards: List[widgets.InfoGrid] = []
        self._gpu_cards: List[ctk.CTkFrame] = []
        self._core_bars: List[CoreBar] = []

        # Состояние диспетчера задач
        self._process_filter_var = tk.StringVar()
        self._process_sort_column = "cpu"
        self._process_sort_reverse = True
        self._process_row_data: Dict[str, ProcessInfo] = {}

        self._ttk_style = ttk.Style(self)
        try:
            self._ttk_style.theme_use("clam")
        except tk.TclError:
            pass
        widgets.style_treeview(self._ttk_style)

        self._pages: Dict[str, ctk.CTkFrame] = {}
        self._build_menu()
        self._build_layout()

        self._refresh_data()
        self._poll_queue()

    # ------------------------------------------------------------------ #
    # Построение интерфейса
    # ------------------------------------------------------------------ #
    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self)
        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="Обновить", command=self._refresh_data, accelerator="F5")
        file_menu.add_command(label="Экспортировать отчёт...", command=self._export_report)
        file_menu.add_separator()
        file_menu.add_command(label="Выход", command=self.destroy)
        menu_bar.add_cascade(label="Файл", menu=file_menu)

        help_menu = tk.Menu(menu_bar, tearoff=False)
        help_menu.add_command(label="О программе", command=self._show_about)
        menu_bar.add_cascade(label="Справка", menu=help_menu)

        self.config(menu=menu_bar)
        self.bind("<F5>", lambda _e: self._refresh_data())

    def _build_layout(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._sidebar = widgets.Sidebar(self, _NAV_ITEMS, on_select=self._show_page)
        self._sidebar.grid(row=0, column=0, sticky="nsw")

        theme_row = ctk.CTkFrame(self._sidebar.bottom_area, fg_color="transparent")
        theme_row.pack(fill="x")
        self._theme_switch = ctk.CTkSwitch(
            theme_row, text="Тёмная тема", font=theme.font("small"), command=self._toggle_theme,
            progress_color=theme.color("accent"),
        )
        self._theme_switch.select()
        self._theme_switch.pack(anchor="w")

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew", padx=(0, 0), pady=0)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=1)

        self._build_toolbar(content)

        router = ctk.CTkFrame(content, fg_color="transparent")
        router.grid(row=1, column=0, sticky="nsew", padx=24, pady=(4, 20))
        router.grid_columnconfigure(0, weight=1)
        router.grid_rowconfigure(0, weight=1)
        self._router = router

        self._pages["dashboard"] = self._build_dashboard_page(router)
        self._pages["os"] = self._build_os_page(router)
        self._pages["cpu"] = self._build_cpu_page(router)
        self._pages["memory"] = self._build_memory_page(router)
        self._pages["disk"] = self._build_disk_page(router)
        self._pages["network"] = self._build_network_page(router)
        self._pages["gpu"] = self._build_gpu_page(router)
        self._pages["motherboard"] = self._build_motherboard_page(router)
        self._pages["battery"] = self._build_battery_page(router)
        self._pages["processes"] = self._build_process_page(router)

        for page in self._pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        self._show_page("dashboard")

    def _build_toolbar(self, parent: ctk.CTkFrame) -> None:
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 0))
        bar.grid_columnconfigure(2, weight=1)

        ctk.CTkButton(
            bar, text="Обновить сейчас", command=self._refresh_data, width=150, height=34,
            corner_radius=theme.RADIUS_SM, fg_color=theme.color("accent"), hover_color=theme.color("accent_hover"),
            font=theme.font("body_bold"),
        ).grid(row=0, column=0, sticky="w")

        self._status_label = ctk.CTkLabel(
            bar, text="Сбор информации о системе...", font=theme.font("small"),
            text_color=theme.color("text_secondary"),
        )
        self._status_label.grid(row=0, column=1, sticky="w", padx=(14, 0))

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=3, sticky="e")

        ctk.CTkSwitch(
            right, text="Авто ЦП/Память", variable=self._live_updates_var, font=theme.font("small"),
            progress_color=theme.color("accent"), onvalue=True, offvalue=False,
        ).pack(side="left", padx=(0, 16))

        ctk.CTkButton(
            right, text="Экспортировать отчёт...", command=self._export_report, width=180, height=34,
            corner_radius=theme.RADIUS_SM, fg_color="transparent", border_width=1,
            border_color=theme.color("card_border"), text_color=theme.color("text_primary"),
            hover_color=theme.color("nav_hover"), font=theme.font("body"),
        ).pack(side="left")

    def _show_page(self, key: str) -> None:
        self._pages[key].tkraise()
        self._sidebar.set_selected(key)
        if self._latest_snapshot is not None:
            self._render_active_page(self._latest_snapshot)

    def _is_process_page_active(self) -> bool:
        return self._sidebar._selected == "processes"  # noqa: SLF001 (внутренний, тот же модуль по смыслу)

    # ------------------------------------------------------------------ #
    # Вспомогательное построение страниц-оболочек
    # ------------------------------------------------------------------ #
    def _make_page_shell(
        self, parent: ctk.CTkFrame, title: str, subtitle: str
    ) -> Tuple[ctk.CTkFrame, ctk.CTkScrollableFrame, widgets.PageHeader]:
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        header = widgets.PageHeader(page, title, subtitle)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))

        scroll = ctk.CTkScrollableFrame(
            page, fg_color="transparent", scrollbar_button_color=theme.color("scrollbar")
        )
        scroll.grid(row=1, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        return page, scroll, header

    # ------------------------------------------------------------------ #
    # Дашборд
    # ------------------------------------------------------------------ #
    def _build_dashboard_page(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        page, scroll, _header = self._make_page_shell(
            parent, "Дашборд", "Ключевые показатели вашей системы в реальном времени"
        )

        hero = ctk.CTkFrame(scroll, fg_color="transparent")
        hero.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        hero.grid_columnconfigure((0, 1), weight=1, uniform="hero")
        self._dash_cpu_card = widgets.HeroStatCard(hero, "Загрузка процессора", icon_name="cpu")
        self._dash_cpu_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        self._dash_mem_card = widgets.HeroStatCard(hero, "Использование ОЗУ", icon_name="memory")
        self._dash_mem_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        stats = ctk.CTkFrame(scroll, fg_color="transparent")
        stats.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        for c in range(4):
            stats.grid_columnconfigure(c, weight=1, uniform="stats")
        self._dash_disk_card = widgets.ProgressStatCard(stats, "Диски (всего)", icon_name="disk")
        self._dash_disk_card.grid(row=0, column=0, sticky="nsew", padx=6)
        self._dash_net_card = widgets.FactCard(stats, "Сеть", icon_name="network")
        self._dash_net_card.grid(row=0, column=1, sticky="nsew", padx=6)
        self._dash_gpu_card = widgets.FactCard(stats, "Видеокарта", icon_name="gpu")
        self._dash_gpu_card.grid(row=0, column=2, sticky="nsew", padx=6)
        self._dash_battery_card = widgets.ProgressStatCard(stats, "Батарея", icon_name="battery")
        self._dash_battery_card.grid(row=0, column=3, sticky="nsew", padx=6)

        summary = widgets.InfoGrid(scroll, title="Обзор системы", icon_name="monitor", columns=2)
        summary.grid(row=2, column=0, sticky="ew")
        self._dash_summary = summary

        return page

    # ------------------------------------------------------------------ #
    # ОС
    # ------------------------------------------------------------------ #
    def _build_os_page(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        page, scroll, _header = self._make_page_shell(
            parent, "Операционная система", "Идентификация ОС и время работы"
        )
        self._os_grid = widgets.InfoGrid(scroll, columns=2)
        self._os_grid.grid(row=0, column=0, sticky="ew")
        return page

    # ------------------------------------------------------------------ #
    # ЦП
    # ------------------------------------------------------------------ #
    def _build_cpu_page(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        page, scroll, _header = self._make_page_shell(
            parent, "Процессор", "Загрузка, частоты и характеристики ЦП"
        )
        self._cpu_hero = widgets.HeroStatCard(scroll, "Общая загрузка ЦП", icon_name="cpu")
        self._cpu_hero.grid(row=0, column=0, sticky="ew", pady=(0, 14))

        self._cpu_specs = widgets.InfoGrid(scroll, title="Характеристики", columns=2)
        self._cpu_specs.grid(row=1, column=0, sticky="ew", pady=(0, 14))

        cores_card = widgets.Card(scroll)
        cores_card.grid(row=2, column=0, sticky="ew")
        cores_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            cores_card, text="Загрузка по ядрам", font=theme.font("card_title"),
            text_color=theme.color("text_secondary"), anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 4))
        self._cores_body = ctk.CTkFrame(cores_card, fg_color="transparent")
        self._cores_body.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 16))
        for c in range(4):
            self._cores_body.grid_columnconfigure(c, weight=1, uniform="cores")
        return page

    # ------------------------------------------------------------------ #
    # Память
    # ------------------------------------------------------------------ #
    def _build_memory_page(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        page, scroll, _header = self._make_page_shell(
            parent, "Память", "Использование ОЗУ и файла подкачки"
        )
        self._mem_hero = widgets.HeroStatCard(scroll, "Использование ОЗУ", icon_name="memory")
        self._mem_hero.grid(row=0, column=0, sticky="ew", pady=(0, 14))

        row2 = ctk.CTkFrame(scroll, fg_color="transparent")
        row2.grid(row=1, column=0, sticky="ew")
        row2.grid_columnconfigure((0, 1), weight=1, uniform="mem2")
        self._swap_card = widgets.ProgressStatCard(row2, "Файл подкачки", icon_name="memory")
        self._swap_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        self._mem_details = widgets.InfoGrid(row2, title="Подробно", columns=1)
        self._mem_details.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        return page

    # ------------------------------------------------------------------ #
    # Диски
    # ------------------------------------------------------------------ #
    def _build_disk_page(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        page, scroll, _header = self._make_page_shell(parent, "Диски", "Использование дисковых разделов")
        body = ctk.CTkFrame(scroll, fg_color="transparent")
        body.grid(row=0, column=0, sticky="ew")
        body.grid_columnconfigure((0, 1), weight=1, uniform="disks")
        self._disk_body = body
        return page

    # ------------------------------------------------------------------ #
    # Сеть
    # ------------------------------------------------------------------ #
    def _build_network_page(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        page, scroll, _header = self._make_page_shell(parent, "Сеть", "Сетевые адаптеры и трафик")
        body = ctk.CTkFrame(scroll, fg_color="transparent")
        body.grid(row=0, column=0, sticky="ew")
        body.grid_columnconfigure((0, 1), weight=1, uniform="net")
        self._network_body = body
        return page

    # ------------------------------------------------------------------ #
    # Видеокарта
    # ------------------------------------------------------------------ #
    def _build_gpu_page(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        page, scroll, _header = self._make_page_shell(parent, "Видеокарта", "Графические адаптеры системы")
        body = ctk.CTkFrame(scroll, fg_color="transparent")
        body.grid(row=0, column=0, sticky="ew")
        body.grid_columnconfigure((0, 1), weight=1, uniform="gpu")
        self._gpu_body = body
        return page

    # ------------------------------------------------------------------ #
    # Материнская плата
    # ------------------------------------------------------------------ #
    def _build_motherboard_page(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        page, scroll, _header = self._make_page_shell(
            parent, "Материнская плата", "Материнская плата и прошивка BIOS/UEFI"
        )
        self._motherboard_grid = widgets.InfoGrid(scroll, columns=2)
        self._motherboard_grid.grid(row=0, column=0, sticky="ew")
        return page

    # ------------------------------------------------------------------ #
    # Батарея
    # ------------------------------------------------------------------ #
    def _build_battery_page(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        page, scroll, _header = self._make_page_shell(parent, "Батарея", "Состояние заряда и питания")
        self._battery_body = ctk.CTkFrame(scroll, fg_color="transparent")
        self._battery_body.grid(row=0, column=0, sticky="ew")
        self._battery_body.grid_columnconfigure(0, weight=1)
        self._battery_widget: Optional[ctk.CTkFrame] = None
        self._battery_widget_kind: Optional[bool] = None
        return page

    # ------------------------------------------------------------------ #
    # Диспетчер задач
    # ------------------------------------------------------------------ #
    def _build_process_page(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)

        header = widgets.PageHeader(page, "Диспетчер задач", "Запущенные процессы, использование ресурсов")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))

        toolbar = widgets.Card(page, corner_radius=theme.RADIUS_SM)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        inner = ctk.CTkFrame(toolbar, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=10)

        search_entry = ctk.CTkEntry(
            inner, textvariable=self._process_filter_var, placeholder_text="Поиск по имени или PID...",
            width=240, height=32, corner_radius=theme.RADIUS_SM,
        )
        search_entry.pack(side="left")
        self._process_filter_var.trace_add("write", lambda *_a: self._refresh_process_view())

        ctk.CTkButton(
            inner, text="Завершить процесс", command=self._terminate_selected_process, height=32,
            corner_radius=theme.RADIUS_SM, fg_color=theme.color("danger"), hover_color=theme.color("danger"),
            font=theme.font("small"), width=150,
        ).pack(side="left", padx=(10, 0))
        ctk.CTkButton(
            inner, text="Свойства...", command=self._show_selected_process_properties, height=32,
            corner_radius=theme.RADIUS_SM, fg_color="transparent", border_width=1,
            border_color=theme.color("card_border"), text_color=theme.color("text_primary"),
            hover_color=theme.color("nav_hover"), font=theme.font("small"), width=110,
        ).pack(side="left", padx=(8, 0))

        self._process_count_label = ctk.CTkLabel(
            inner, text="", font=theme.font("small"), text_color=theme.color("text_secondary")
        )
        self._process_count_label.pack(side="right")

        table_card = widgets.Card(page, corner_radius=theme.RADIUS_SM)
        table_card.grid(row=2, column=0, sticky="nsew")
        table_card.grid_columnconfigure(0, weight=1)
        table_card.grid_rowconfigure(0, weight=1)

        table_frame = tk.Frame(table_card, bg=theme.resolve("card_bg"))
        table_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        columns = [key for key, _label, _width in PROCESS_COLUMNS]
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        for key, label, width in PROCESS_COLUMNS:
            tree.heading(key, text=label, command=lambda c=key: self._sort_process_view(c))
            anchor = tk.W if key in ("name", "user", "status") else tk.E
            tree.column(key, width=width, anchor=anchor)
        tree.tag_configure("high", foreground=theme.resolve("danger"))
        tree.tag_configure("warn", foreground=theme.resolve("warning"))

        v_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=tree.yview)
        h_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")

        tree.bind("<Double-1>", lambda _e: self._show_selected_process_properties())
        tree.bind("<Button-3>", self._show_process_context_menu)
        self._process_tree = tree
        self._process_table_frame = table_frame

        self._process_context_menu = tk.Menu(self, tearoff=False)
        self._process_context_menu.add_command(label="Свойства...", command=self._show_selected_process_properties)
        self._process_context_menu.add_separator()
        self._process_context_menu.add_command(label="Завершить процесс", command=self._terminate_selected_process)
        self._process_context_menu.add_command(
            label="Завершить принудительно", command=lambda: self._terminate_selected_process(force=True)
        )
        return page

    def _center_toplevel(self, win: ctk.CTkToplevel, width: int, height: int) -> None:
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - width) // 2
        y = self.winfo_y() + (self.winfo_height() - height) // 2
        win.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    # ------------------------------------------------------------------ #
    # Тема оформления
    # ------------------------------------------------------------------ #
    def _toggle_theme(self) -> None:
        is_dark = bool(self._theme_switch.get())
        ctk.set_appearance_mode("Dark" if is_dark else "Light")
        widgets.style_treeview(self._ttk_style)
        widgets.refresh_all_canvas_themes()
        self._process_table_frame.configure(bg=theme.resolve("card_bg"))

    # ------------------------------------------------------------------ #
    # Обновление данных: фоновый поток + потокобезопасная очередь
    # ------------------------------------------------------------------ #
    def _ensure_worker_running(self) -> None:
        if not self._worker_started:
            self._worker_started = True
            threading.Thread(target=self._collect_in_background, daemon=True).start()

    def _refresh_data(self) -> None:
        if self._is_first_run:
            self._status_label.configure(text="Сбор информации о системе...")
        self._ensure_worker_running()

    def _collect_in_background(self) -> None:
        import platform
        import time

        com_initialized = False
        if platform.system() == "Windows":
            try:
                import pythoncom

                pythoncom.CoInitialize()
                com_initialized = True
            except Exception as exc:
                logger.debug("Не удалось инициализировать pythoncom: %s", exc)

        try:
            while True:
                try:
                    want_processes = self._is_first_run or self._is_process_page_active()
                    snapshot = self._collector.collect_all(
                        is_first_run=self._is_first_run, collect_processes=want_processes
                    )
                    self._result_queue.put(snapshot)
                    if self._is_first_run:
                        self._is_first_run = False
                    time.sleep(1.0)
                except Exception:
                    logger.exception("Ошибка при сборе данных")
                    time.sleep(1.0)
        finally:
            if com_initialized:
                try:
                    import pythoncom

                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def _poll_queue(self) -> None:
        self._ensure_worker_running()
        snapshot = None
        while True:
            try:
                snapshot = self._result_queue.get_nowait()
            except queue.Empty:
                break
        if snapshot:
            self._latest_snapshot = snapshot
            self._populate_ui(snapshot)
            self._status_label.configure(text=f"Обновлено: {snapshot.timestamp}")
        self.after(1000, self._poll_queue)

    # ------------------------------------------------------------------ #
    # Заполнение виджетов данными снимка
    # ------------------------------------------------------------------ #
    def _populate_ui(self, s: SystemSnapshot) -> None:
        # Дешёвая часть (без обращения к виджетам) выполняется всегда, чтобы
        # история для спарклайнов накапливалась непрерывно даже когда
        # дашборд/страница ЦП/памяти сейчас не на экране — иначе при
        # переключении на них график каждый раз "прогревался" бы заново.
        if self._live_updates_var.get():
            total, cores = self._update_smoothed_cpu(s)
            self._cpu_history.append(total)
            self._mem_history.append(s.memory_info.used_percent)
            self._last_cpu_total, self._last_cpu_cores = total, cores

        # Дорогая часть — обновление самих виджетов (Canvas-перерисовка
        # спарклайнов/колец, реконфигурация карточек и строк Treeview) —
        # выполняется ТОЛЬКО для страницы, которая сейчас видна. Раньше
        # здесь безусловно перерисовывались виджеты всех 10 страниц каждую
        # секунду, включая скрытые — это и было основной причиной лагов.
        self._render_active_page(s)

    def _render_active_page(self, s: Optional[SystemSnapshot]) -> None:
        """Обновляет виджеты только текущей активной страницы данными
        из переданного снимка. Вызывается и по таймеру (раз в секунду —
        только для видимой страницы), и сразу при переключении страницы
        (чтобы новая страница отобразила данные мгновенно, а не через
        секунду ожидания следующего цикла опроса)."""
        if s is None:
            return
        live = self._live_updates_var.get()
        key = self._sidebar._selected  # noqa: SLF001 (внутренний, тот же модуль по смыслу)

        if key == "dashboard":
            self._populate_dashboard_static(s)
            if live:
                self._populate_cpu_visuals(s, self._last_cpu_total, self._last_cpu_cores, target="dash")
                self._populate_memory_visuals(s, target="dash")
        elif key == "os":
            self._os_grid.set_rows(self._os_rows(s))
        elif key == "cpu":
            if live:
                self._populate_cpu_visuals(s, self._last_cpu_total, self._last_cpu_cores, target="detail")
        elif key == "memory":
            if live:
                self._populate_memory_visuals(s, target="detail")
        elif key == "disk":
            self._populate_disks(s)
        elif key == "network":
            self._populate_network(s)
        elif key == "gpu":
            self._populate_gpus(s)
        elif key == "motherboard":
            self._motherboard_grid.set_rows(self._motherboard_rows(s))
        elif key == "battery":
            self._populate_battery(s)
        elif key == "processes":
            self._populate_process_tab(s)

    def _update_smoothed_cpu(self, s: SystemSnapshot) -> Tuple[float, List[float]]:
        c = s.cpu_info
        current = list(c.per_core_usage_percent)
        is_all_zero = all(v == 0.0 for v in current)

        if self._smoothed_cpu_cores is not None:
            if is_all_zero:
                current = self._smoothed_cpu_cores
            else:
                alpha = 0.4
                current = [
                    round(prev + alpha * (new - prev), 1) for prev, new in zip(self._smoothed_cpu_cores, current)
                ]
        self._smoothed_cpu_cores = current
        total = round(sum(current) / len(current), 1) if current else 0.0
        return total, current

    def _populate_cpu_visuals(
        self, s: SystemSnapshot, total: float, cores: List[float], target: str = "both"
    ) -> None:
        c = s.cpu_info
        detail = f"{c.name}"
        subtitle = f"{c.physical_cores} физ. / {c.logical_cores} лог. ядер • {c.current_frequency_mhz:.0f} МГц"
        history = list(self._cpu_history)

        if target in ("dash", "both"):
            self._dash_cpu_card.update(total, detail, subtitle, history)

        if target not in ("detail", "both"):
            return

        self._cpu_hero.update(total, detail, subtitle, history)

        self._cpu_specs.set_rows(
            [
                ("Модель", c.name),
                ("Физические ядра", str(c.physical_cores)),
                ("Логические ядра", str(c.logical_cores)),
                ("Текущая частота", f"{c.current_frequency_mhz} МГц"),
                ("Минимальная частота", f"{c.min_frequency_mhz} МГц"),
                ("Максимальная частота", f"{c.max_frequency_mhz} МГц"),
            ]
        )

        while len(self._core_bars) < len(cores):
            idx = len(self._core_bars)
            bar = CoreBar(self._cores_body)
            r, col = divmod(idx, 4)
            bar.grid(row=r, column=col, sticky="ew", padx=6)
            self._core_bars.append(bar)
        while len(self._core_bars) > len(cores):
            self._core_bars.pop().destroy()
        for i, (bar, pct) in enumerate(zip(self._core_bars, cores)):
            bar.update_core(i, pct)

    def _populate_memory_visuals(self, s: SystemSnapshot, target: str = "both") -> None:
        m = s.memory_info
        detail = f"{m.used_gb} / {m.total_gb} ГБ"
        subtitle = f"Доступно: {m.available_gb} ГБ"
        history = list(self._mem_history)

        if target in ("dash", "both"):
            self._dash_mem_card.update(m.used_percent, detail, subtitle, history)

        if target not in ("detail", "both"):
            return

        self._mem_hero.update(m.used_percent, detail, subtitle, history)

        swap_pct = m.swap_used_percent
        self._swap_card.update(
            f"{m.swap_used_gb} / {m.swap_total_gb} ГБ", swap_pct, f"Свободно подкачки: {m.swap_free_gb} ГБ",
            muted=m.swap_total_gb <= 0,
        )
        self._mem_details.set_rows(
            [
                ("Всего ОЗУ", f"{m.total_gb} ГБ"),
                ("Использовано", f"{m.used_gb} ГБ ({m.used_percent} %)"),
                ("Доступно", f"{m.available_gb} ГБ"),
                ("Подкачка всего", f"{m.swap_total_gb} ГБ"),
            ]
        )

    def _populate_dashboard_static(self, s: SystemSnapshot) -> None:
        total_used = sum(d.used_gb for d in s.disks)
        total_cap = sum(d.total_gb for d in s.disks)
        pct = round((total_used / total_cap) * 100, 1) if total_cap > 0 else 0.0
        self._dash_disk_card.update(
            f"{round(total_used, 1)} / {round(total_cap, 1)} ГБ", pct, f"Разделов: {len(s.disks)}"
        )

        active = [n for n in s.network_interfaces if n.is_up]
        sent = round(sum(n.bytes_sent_mb for n in s.network_interfaces), 1)
        recv = round(sum(n.bytes_recv_mb for n in s.network_interfaces), 1)
        self._dash_net_card.update(f"{len(active)} активно", f"\u2191 {sent} МБ · \u2193 {recv} МБ")

        if s.gpus:
            g = s.gpus[0]
            self._dash_gpu_card.update(g.name, f"Драйвер {g.driver_version}")
        else:
            self._dash_gpu_card.update("Н/Д", "Видеокарта не обнаружена")

        b = s.battery
        if b.present:
            self._dash_battery_card.update(
                f"{b.percent:.0f}%", b.percent, "Питание от сети" if b.plugged_in else f"Осталось: {b.time_left}"
            )
        else:
            self._dash_battery_card.update("—", 0, "Настольный компьютер", muted=True)

        self._dash_summary.set_rows(
            [
                ("Операционная система", f"{s.os_info.system} {s.os_info.release}"),
                ("Имя компьютера", s.os_info.node_name),
                ("Материнская плата", f"{s.motherboard.manufacturer} {s.motherboard.product}"),
                ("Время работы системы", s.os_info.uptime),
                ("Установленная ОЗУ", f"{s.memory_info.total_gb} ГБ"),
                ("Отчёт сформирован", s.timestamp),
            ]
        )

    def _populate_disks(self, s: SystemSnapshot) -> None:
        def factory(container):
            return widgets.ProgressStatCard(container, "Диск", icon_name="disk")

        self._disk_cards = self._sync_cards(self._disk_body, self._disk_cards, len(s.disks), factory, columns=2)
        for card, d in zip(self._disk_cards, s.disks):
            card.update(
                f"{d.used_gb} / {d.total_gb} ГБ", d.used_percent,
                f"{d.device} • {d.mountpoint} • {d.file_system} • своб. {d.free_gb} ГБ",
            )

    def _populate_network(self, s: SystemSnapshot) -> None:
        def factory(container):
            return widgets.InfoGrid(container, columns=1)

        self._network_cards = self._sync_cards(
            self._network_body, self._network_cards, len(s.network_interfaces), factory, columns=2
        )
        for card, n in zip(self._network_cards, s.network_interfaces):
            card.set_rows(
                [
                    ("Адаптер", n.name),
                    ("Статус", "Активен" if n.is_up else "Отключён"),
                    ("IP-адрес", n.ip_address),
                    ("MAC-адрес", n.mac_address),
                    ("Отправлено", f"{n.bytes_sent_mb} МБ"),
                    ("Получено", f"{n.bytes_recv_mb} МБ"),
                ]
            )

    def _populate_gpus(self, s: SystemSnapshot) -> None:
        def factory(container):
            return widgets.ProgressStatCard(container, "Видеокарта", icon_name="gpu")

        self._gpu_cards = self._sync_cards(self._gpu_body, self._gpu_cards, len(s.gpus), factory, columns=2)
        for card, g in zip(self._gpu_cards, s.gpus):
            has_vram = g.adapter_ram_gb > 0
            pct = round((g.used_ram_gb / g.adapter_ram_gb) * 100, 1) if has_vram else 0.0
            value = f"{g.used_ram_gb} / {g.adapter_ram_gb} ГБ" if has_vram else "Видеопамять: Н/Д"
            card.update(
                value, pct, f"{g.name} • драйвер {g.driver_version} • {g.resolution}", muted=not has_vram
            )

    def _populate_battery(self, s: SystemSnapshot) -> None:
        b = s.battery
        if getattr(self, "_battery_widget", None) is None or self._battery_widget_kind != b.present:
            for child in self._battery_body.winfo_children():
                child.destroy()
            if not b.present:
                self._battery_widget = widgets.FactCard(self._battery_body, "Батарея", icon_name="battery")
            else:
                self._battery_widget = widgets.ProgressStatCard(
                    self._battery_body, "Заряд батареи", icon_name="battery"
                )
            self._battery_widget.grid(row=0, column=0, sticky="ew")
            self._battery_widget_kind = b.present

        if not b.present:
            self._battery_widget.update("Не обнаружена", "Настольный компьютер без аккумулятора")
        else:
            status = "Питание от сети" if b.plugged_in else "Работает от батареи"
            self._battery_widget.update(f"{b.percent:.0f}%", b.percent, f"{status} • Осталось: {b.time_left}")

    # ------------------------------------------------------------------ #
    # Общий помощник: синхронизация количества динамических карточек
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sync_cards(container, existing: list, count: int, factory: Callable, columns: int) -> list:
        while len(existing) < count:
            idx = len(existing)
            card = factory(container)
            r, c = divmod(idx, columns)
            card.grid(row=r, column=c, sticky="nsew", padx=6, pady=6)
            existing.append(card)
        while len(existing) > count:
            existing.pop().destroy()
        return existing

    # ------------------------------------------------------------------ #
    # Строки "параметр -> значение" (переиспользуются также в текстовом
    # отчёте при экспорте).
    # ------------------------------------------------------------------ #
    @staticmethod
    def _os_rows(s: SystemSnapshot) -> List[Tuple[str, str]]:
        o = s.os_info
        return [
            ("Система", o.system),
            ("Имя узла", o.node_name),
            ("Релиз", o.release),
            ("Версия", o.version),
            ("Машина", o.machine),
            ("Разрядность", o.architecture),
            ("Процессор", o.processor),
            ("Время загрузки", o.boot_time),
            ("Время работы", o.uptime),
        ]

    @staticmethod
    def _motherboard_rows(s: SystemSnapshot) -> List[Tuple[str, str]]:
        b = s.motherboard
        return [
            ("Производитель", b.manufacturer),
            ("Модель", b.product),
            ("Производитель BIOS", b.bios_vendor),
            ("Версия BIOS", b.bios_version),
            ("Серийный номер", b.serial_number),
        ]

    # ------------------------------------------------------------------ #
    # Диспетчер задач: заполнение, сортировка, фильтр
    # ------------------------------------------------------------------ #
    def _populate_process_tab(self, s: SystemSnapshot) -> None:
        processes = list(s.processes)
        query = self._process_filter_var.get().strip().lower()
        if query:
            processes = [p for p in processes if query in p.name.lower() or query == str(p.pid)]

        key_func = _PROCESS_SORT_KEYS.get(self._process_sort_column, _PROCESS_SORT_KEYS["cpu"])
        processes.sort(key=key_func, reverse=self._process_sort_reverse)
        self._process_row_data = {str(p.pid): p for p in processes}

        tree = self._process_tree
        current_map = {tree.item(iid, "values")[0]: iid for iid in tree.get_children()}
        for p in processes:
            tag = "high" if p.cpu_percent >= 85 else "warn" if p.cpu_percent >= 60 else ""
            row = (
                str(p.pid), p.name, p.username, p.status, p.cpu_percent, p.memory_mb, p.memory_percent,
                p.disk_read_kb_s, p.disk_write_kb_s, p.network_connections, p.num_threads,
            )
            key = row[0]
            if key in current_map:
                iid = current_map.pop(key)
                tree.item(iid, values=row, tags=(tag,) if tag else ())
            else:
                tree.insert("", tk.END, values=row, tags=(tag,) if tag else ())
        for iid in current_map.values():
            tree.delete(iid)

        self._process_count_label.configure(text=f"Процессов: {len(processes)}")

    def _refresh_process_view(self) -> None:
        if self._latest_snapshot is not None:
            self._populate_process_tab(self._latest_snapshot)

    def _sort_process_view(self, column: str) -> None:
        if self._process_sort_column == column:
            self._process_sort_reverse = not self._process_sort_reverse
        else:
            self._process_sort_column = column
            self._process_sort_reverse = column in _PROCESS_DESCENDING_BY_DEFAULT
        self._refresh_process_view()

    def _get_selected_pid(self) -> Optional[int]:
        selection = self._process_tree.selection()
        if not selection:
            return None
        values = self._process_tree.item(selection[0], "values")
        if not values:
            return None
        try:
            return int(values[0])
        except (ValueError, IndexError):
            return None

    def _show_process_context_menu(self, event: tk.Event) -> None:
        tree = self._process_tree
        row_id = tree.identify_row(event.y)
        if not row_id:
            return
        tree.selection_set(row_id)
        self._process_context_menu.tk_popup(event.x_root, event.y_root)

    def _terminate_selected_process(self, force: bool = False) -> None:
        pid = self._get_selected_pid()
        if pid is None:
            messagebox.showinfo(APP_TITLE, "Сначала выберите процесс в списке.")
            return

        proc_info = self._process_row_data.get(str(pid))
        proc_name = proc_info.name if proc_info else str(pid)
        action = "принудительно завершить" if force else "завершить"
        confirmed = messagebox.askyesno(
            APP_TITLE,
            f"Вы действительно хотите {action} процесс «{proc_name}» (PID {pid})?\n\n"
            "Все несохранённые данные в этом процессе будут потеряны.",
            icon="warning",
        )
        if not confirmed:
            return

        def _do_terminate() -> None:
            success, message = self._collector.terminate_process(pid, force=force)
            self.after(0, lambda: self._on_terminate_done(success, message))

        threading.Thread(target=_do_terminate, daemon=True).start()

    def _on_terminate_done(self, success: bool, message: str) -> None:
        if success:
            messagebox.showinfo(APP_TITLE, message)
        else:
            messagebox.showerror(APP_TITLE, message)

    def _show_selected_process_properties(self) -> None:
        pid = self._get_selected_pid()
        if pid is None:
            messagebox.showinfo(APP_TITLE, "Сначала выберите процесс в списке.")
            return
        live_info = self._process_row_data.get(str(pid))

        def _fetch() -> None:
            details = self._collector.get_process_details(pid)
            self.after(0, lambda: self._render_process_properties(pid, details, live_info))

        threading.Thread(target=_fetch, daemon=True).start()

    def _render_process_properties(
        self, pid: int, details: Optional[ProcessDetails], live_info: Optional[ProcessInfo]
    ) -> None:
        if details is None:
            messagebox.showwarning(APP_TITLE, f"Процесс с PID {pid} уже завершён или недоступен.")
            return

        win = ctk.CTkToplevel(self)
        win.title(f"Свойства процесса — {details.name} (PID {pid})")
        win.transient(self)
        win.configure(fg_color=theme.color("app_bg"))
        self._center_toplevel(win, 520, 560)

        card = widgets.Card(win, corner_radius=theme.RADIUS_SM)
        card.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=12, pady=12)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)

        tree_frame = tk.Frame(card, bg=theme.resolve("card_bg"))
        tree_frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        tree = ttk.Treeview(tree_frame, columns=("property", "value"), show="headings")
        tree.heading("property", text="Параметр")
        tree.heading("value", text="Значение")
        tree.column("property", width=190, anchor=tk.W)
        tree.column("value", width=280, anchor=tk.W)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")

        rows: List[Tuple[str, str]] = [
            ("PID", str(details.pid)),
            ("Родительский процесс", f"{details.parent_name} (PID {details.ppid})"),
            ("Имя", details.name),
            ("Состояние", details.status),
            ("Пользователь", details.username),
            ("Путь к исполняемому файлу", details.exe_path),
            ("Командная строка", details.cmdline),
            ("Рабочий каталог", details.working_directory),
            ("Запущен", details.create_time),
            ("Приоритет", details.priority),
            ("Потоков", str(details.num_threads)),
            ("Открытых файлов", str(details.open_files)),
            ("Сетевых подключений", str(details.network_connections)),
            ("Время ЦП (пользователь)", f"{details.cpu_user_time_s} с"),
            ("Время ЦП (система)", f"{details.cpu_system_time_s} с"),
            ("Память RSS (рабочий набор)", f"{details.memory_rss_mb} МБ"),
            ("Память VMS (виртуальная)", f"{details.memory_vms_mb} МБ"),
            ("Использовано памяти", f"{details.memory_percent} %"),
            ("Прочитано с диска (всего)", f"{details.disk_read_mb_total} МБ"),
            ("Записано на диск (всего)", f"{details.disk_write_mb_total} МБ"),
        ]
        if live_info is not None:
            rows.extend(
                [
                    ("— Текущая загрузка ЦП", f"{live_info.cpu_percent} %"),
                    ("— Текущая память", f"{live_info.memory_mb} МБ"),
                    ("— Диск: чтение сейчас", f"{live_info.disk_read_kb_s} КБ/с"),
                    ("— Диск: запись сейчас", f"{live_info.disk_write_kb_s} КБ/с"),
                ]
            )
        for row in rows:
            tree.insert("", tk.END, values=row)

        button_bar = ctk.CTkFrame(win, fg_color="transparent")
        button_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 12))
        ctk.CTkButton(
            button_bar, text="Завершить процесс", fg_color=theme.color("danger"), hover_color=theme.color("danger"),
            command=lambda: (win.destroy(), self._terminate_selected_process()),
        ).pack(side=tk.LEFT)
        ctk.CTkButton(
            button_bar, text="Закрыть", fg_color="transparent", border_width=1,
            border_color=theme.color("card_border"), text_color=theme.color("text_primary"),
            hover_color=theme.color("nav_hover"), command=win.destroy,
        ).pack(side=tk.RIGHT)

    # ------------------------------------------------------------------ #
    # Экспорт / О программе
    # ------------------------------------------------------------------ #
    def _export_report(self) -> None:
        if self._latest_snapshot is None:
            messagebox.showwarning(APP_TITLE, "Данные ещё не готовы. Дождитесь первого обновления.")
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Файл JSON", "*.json"), ("Текстовый файл", "*.txt")],
            initialfile=f"otchet_o_sisteme_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        )
        if not file_path:
            return
        try:
            if file_path.lower().endswith(".json"):
                with open(file_path, "w", encoding="utf-8") as fh:
                    json.dump(asdict(self._latest_snapshot), fh, indent=2, ensure_ascii=False)
            else:
                with open(file_path, "w", encoding="utf-8") as fh:
                    fh.write(self._render_text_report(self._latest_snapshot))
            messagebox.showinfo(APP_TITLE, f"Отчёт экспортирован в:\n{file_path}")
        except OSError as exc:
            logger.error("Не удалось экспортировать отчёт: %s", exc)
            messagebox.showerror(APP_TITLE, f"Не удалось экспортировать отчёт:\n{exc}")

    def _render_text_report(self, s: SystemSnapshot) -> str:
        cpu_total, cpu_cores = (
            (self._cpu_history[-1], self._smoothed_cpu_cores) if self._cpu_history and self._smoothed_cpu_cores
            else self._update_smoothed_cpu(s)
        )
        m = s.memory_info
        lines = [f"Отчёт о системе — сформирован {s.timestamp}", "=" * 50]
        sections: List[Tuple[str, List[Tuple[str, str]]]] = [
            ("Операционная система", self._os_rows(s)),
            (
                "Процессор",
                [
                    ("Модель", s.cpu_info.name),
                    ("Физические ядра", str(s.cpu_info.physical_cores)),
                    ("Логические ядра", str(s.cpu_info.logical_cores)),
                    ("Общая загрузка", f"{cpu_total} %"),
                ]
                + [(f"  └─ Загрузка ядра {i}", f"{v} %") for i, v in enumerate(cpu_cores)],
            ),
            (
                "Память",
                [
                    ("Всего ОЗУ", f"{m.total_gb} ГБ"),
                    ("Использовано ОЗУ", f"{m.used_gb} ГБ"),
                    ("Использовано %", f"{m.used_percent} %"),
                    ("Подкачка использовано", f"{m.swap_used_gb} / {m.swap_total_gb} ГБ"),
                ],
            ),
            ("Материнская плата / BIOS", self._motherboard_rows(s)),
            (
                "Батарея",
                [("Статус", "Батарея не обнаружена (настольный компьютер)")]
                if not s.battery.present
                else [
                    ("Заряд", f"{s.battery.percent} %"),
                    ("Питание от сети", "Да" if s.battery.plugged_in else "Нет"),
                    ("Осталось времени", s.battery.time_left),
                ],
            ),
        ]
        for title, rows in sections:
            lines.append(f"\n[{title}]")
            lines.extend(f"{k}: {v}" for k, v in rows)

        lines.append("\n[Диски]")
        for d in s.disks:
            lines.append(f"{d.device} ({d.mountpoint}) — использовано {d.used_gb}/{d.total_gb} ГБ ({d.used_percent}%)")

        lines.append("\n[Сетевые интерфейсы]")
        for n in s.network_interfaces:
            lines.append(f"{n.name}: {n.ip_address} / {n.mac_address} — {'активен' if n.is_up else 'отключен'}")

        lines.append("\n[Видеокарта]")
        for g in s.gpus:
            lines.append(
                f"{g.name} (драйвер {g.driver_version}, видеопамять всего: {g.adapter_ram_gb} ГБ / "
                f"использовано: {g.used_ram_gb} ГБ)"
            )

        lines.append("\n[Топ-10 процессов по загрузке ЦП]")
        top_processes = sorted(s.processes, key=lambda p: p.cpu_percent, reverse=True)[:10]
        for p in top_processes:
            lines.append(f"PID {p.pid} — {p.name}: ЦП {p.cpu_percent}%, память {p.memory_mb} МБ ({p.memory_percent}%)")

        return "\n".join(lines)

    def _show_about(self) -> None:
        win = ctk.CTkToplevel(self)
        win.title("О программе")
        win.transient(self)
        win.configure(fg_color=theme.color("app_bg"))
        win.resizable(False, False)
        self._center_toplevel(win, 440, 340)

        card = widgets.Card(win, corner_radius=theme.RADIUS)
        card.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(card, text="", image=icons.get_icon_accent("dashboard", theme.ACCENT_SOLID, 40)).pack(
            pady=(24, 8)
        )
        ctk.CTkLabel(card, text=APP_TITLE, font=theme.font("section"), text_color=theme.color("text_primary")).pack()
        ctk.CTkLabel(
            card,
            text=(
                "Лёгкая утилита для просмотра подробной информации об оборудовании "
                "и операционной системе, включая встроенный диспетчер задач "
                "(просмотр, завершение и свойства процессов)."
            ),
            font=theme.font("body"), text_color=theme.color("text_secondary"), justify="center", wraplength=360,
        ).pack(pady=(10, 6), padx=20)
        ctk.CTkLabel(
            card, text="Создано с использованием Python, CustomTkinter, psutil и WMI.",
            font=theme.font("small"), text_color=theme.color("text_muted"), justify="center", wraplength=360,
        ).pack(pady=(0, 16))
        ctk.CTkButton(card, text="Закрыть", command=win.destroy, width=120).pack(pady=(0, 20))
