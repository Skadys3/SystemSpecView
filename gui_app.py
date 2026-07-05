"""
gui_app.py

Графический интерфейс приложения «Просмотр характеристик компьютера»
на основе Tkinter. Отображает информацию, собранную
``collectors.SystemInfoCollector``, в окне с вкладками, с возможностью
живого обновления CPU/памяти и экспортом в JSON или обычный текст.

Сбор данных выполняется в фоновом потоке, чтобы более медленные операции
(запросы WMI, замер загрузки CPU) не «замораживали» интерфейс; результаты
передаются обратно в главный поток через потокобезопасную очередь — это
стандартный и безопасный способ сочетать многопоточность с Tkinter.
"""

import json
import logging
import queue
import threading
import tkinter as tk
from dataclasses import asdict
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Tuple

from collectors import SystemInfoCollector
from models import ProcessDetails, ProcessInfo, SystemSnapshot

logger = logging.getLogger(__name__)

APP_TITLE = "Просмотр характеристик компьютера"
APP_MIN_WIDTH = 900
APP_MIN_HEIGHT = 600
LIVE_REFRESH_INTERVAL_MS = 1000
QUEUE_POLL_INTERVAL_MS = 150
CPU_SMOOTHING_FACTOR = 0.12

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

# Ключи сортировки для каждого столбца диспетчера задач
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

# Столбцы, для которых при первом клике логично сортировать по убыванию
# (наибольшая нагрузка — самый интересный процесс — должна быть сверху).
_PROCESS_DESCENDING_BY_DEFAULT = {"cpu", "mem_mb", "mem_pct", "disk_read", "disk_write", "net", "threads"}


class SystemInfoApp(tk.Tk):
    """Главное окно приложения «Просмотр характеристик компьютера»."""

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(APP_MIN_WIDTH, APP_MIN_HEIGHT)
        self.geometry(f"{APP_MIN_WIDTH}x{APP_MIN_HEIGHT}")

        self._collector = SystemInfoCollector()
        self._result_queue: "queue.Queue[SystemSnapshot]" = queue.Queue()
        self._latest_snapshot: Optional[SystemSnapshot] = None
        self._live_updates_enabled = tk.BooleanVar(value=True)
        self._tabs: Dict[str, ttk.Treeview] = {}

        self._is_first_run = True
        self._cached_gpus = None
        self._cached_motherboard = None

        # Состояние вкладки «Диспетчер задач»
        self._process_filter_var = tk.StringVar()
        self._process_sort_column = "cpu"
        self._process_sort_reverse = True
        self._process_row_data: Dict[str, ProcessInfo] = {}
        self._process_tab_frame: Optional[ttk.Frame] = None
        self._notebook: Optional[ttk.Notebook] = None

        self._build_style()
        self._build_menu()
        self._build_layout()

        self._refresh_data()
        self._poll_queue()
        self._smoothed_cpu_data = None
        self._smoothed_cpu_total = 0.0
        self._smoothed_cpu_cores = {}
        self._poll_queue()

    # ------------------------------------------------------------------ #
    # Построение интерфейса
    # ------------------------------------------------------------------ #
    def _build_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        style.configure("Treeview", rowheight=24, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 9))

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
        self.bind("<F5>", lambda _event: self._refresh_data())

    def _build_layout(self) -> None:
        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text="Обновить сейчас", command=self._refresh_data).pack(side=tk.LEFT)
        ttk.Checkbutton(
            toolbar,
            text="Автообновление процессора и памяти",
            variable=self._live_updates_enabled,
        ).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(toolbar, text="Экспортировать отчёт...", command=self._export_report).pack(side=tk.RIGHT)

        notebook = ttk.Notebook(self)
        notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        self._notebook = notebook
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._tabs["overview"] = self._make_kv_tab(notebook, "Обзор")
        self._tabs["os"] = self._make_kv_tab(notebook, "Операционная система")
        self._tabs["cpu"] = self._make_kv_tab(notebook, "Процессор")
        self._tabs["memory"] = self._make_kv_tab(notebook, "Память")
        self._tabs["disk"] = self._make_table_tab(
            notebook,
            "Диски",
            ("Устройство", "Точка монтирования", "Файловая система", "Всего (ГБ)", "Использовано (ГБ)", "Свободно (ГБ)", "Использовано %"),
        )
        self._tabs["network"] = self._make_table_tab(
            notebook,
            "Сеть",
            ("Интерфейс", "IP-адрес", "MAC-адрес", "Отправлено (МБ)", "Получено (МБ)", "Статус"),
        )
        self._tabs["gpu"] = self._make_table_tab(
            notebook, 
            "Видеокарта", 
            ("Название", "Версия драйвера", "Всего памяти", "Использовано памяти", "Разрешение")
        )
        self._tabs["motherboard"] = self._make_kv_tab(notebook, "Материнская плата / BIOS")
        self._tabs["battery"] = self._make_kv_tab(notebook, "Батарея")
        self._tabs["processes"] = self._make_process_tab(notebook)

        status_bar = ttk.Frame(self, padding=(8, 4))
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self._status_label = ttk.Label(status_bar, text="Готово", style="Status.TLabel")
        self._status_label.pack(side=tk.LEFT)

    def _make_kv_tab(self, notebook: ttk.Notebook, label: str) -> ttk.Treeview:
        """Создаёт вкладку с двумя столбцами (параметр/значение)."""
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text=label)
        tree = ttk.Treeview(frame, columns=("property", "value"), show="headings")
        tree.heading("property", text="Параметр")
        tree.heading("value", text="Значение")
        tree.column("property", width=260, anchor=tk.W)
        tree.column("value", width=480, anchor=tk.W)
        self._attach_scrollbar(frame, tree)
        return tree

    def _make_table_tab(self, notebook: ttk.Notebook, label: str, columns: Tuple[str, ...]) -> ttk.Treeview:
        """Создаёт вкладку-таблицу с несколькими строками (по одной на диск/интерфейс/видеокарту)."""
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text=label)
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=130, anchor=tk.W)
        self._attach_scrollbar(frame, tree)
        return tree

    @staticmethod
    def _attach_scrollbar(parent: ttk.Frame, tree: ttk.Treeview) -> None:
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _make_process_tab(self, notebook: ttk.Notebook) -> ttk.Treeview:
        """Создаёт вкладку «Диспетчер задач»: панель поиска/кнопок и таблицу процессов."""
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="Диспетчер задач")
        self._process_tab_frame = frame

        toolbar = ttk.Frame(frame)
        toolbar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

        ttk.Label(toolbar, text="Поиск:").pack(side=tk.LEFT)
        search_entry = ttk.Entry(toolbar, textvariable=self._process_filter_var, width=25)
        search_entry.pack(side=tk.LEFT, padx=(4, 12))
        self._process_filter_var.trace_add("write", lambda *_args: self._refresh_process_view())

        ttk.Button(toolbar, text="Завершить процесс", command=self._terminate_selected_process).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Свойства...", command=self._show_selected_process_properties).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        self._process_count_label = ttk.Label(toolbar, text="", style="Status.TLabel")
        self._process_count_label.pack(side=tk.RIGHT)

        table_frame = ttk.Frame(frame)
        table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        columns = [key for key, _label, _width in PROCESS_COLUMNS]
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        for key, label, width in PROCESS_COLUMNS:
            tree.heading(key, text=label, command=lambda c=key: self._sort_process_view(c))
            anchor = tk.W if key in ("name", "user", "status") else tk.E
            tree.column(key, width=width, anchor=anchor)

        v_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=tree.yview)
        h_scroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)

        tree.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)

        tree.bind("<Double-1>", lambda _event: self._show_selected_process_properties())
        tree.bind("<Button-3>", self._show_process_context_menu)

        self._process_context_menu = tk.Menu(self, tearoff=False)
        self._process_context_menu.add_command(label="Свойства...", command=self._show_selected_process_properties)
        self._process_context_menu.add_separator()
        self._process_context_menu.add_command(label="Завершить процесс", command=self._terminate_selected_process)
        self._process_context_menu.add_command(
            label="Завершить принудительно", command=lambda: self._terminate_selected_process(force=True)
        )

        return tree

    def _on_tab_changed(self, _event: object = None) -> None:
        """При переключении на вкладку «Диспетчер задач» сразу отрисовывает
        самые свежие уже собранные данные (не дожидаясь следующего тика)."""
        if self._is_process_tab_active() and self._latest_snapshot is not None:
            self._populate_process_tab(self._latest_snapshot)

    def _is_process_tab_active(self) -> bool:
        if self._notebook is None or self._process_tab_frame is None:
            return False
        return self._notebook.select() == str(self._process_tab_frame)

    # ------------------------------------------------------------------ #
    # Обновление данных: фоновый поток + потокобезопасная очередь
    # ------------------------------------------------------------------ #
    def _ensure_worker_running(self) -> None:
        """Гарантирует, что бесконечный фоновый поток сбора данных запущен
        РОВНО ОДИН раз за всё время жизни приложения.
        """
        if not getattr(self, "_worker_started", False):
            self._worker_started = True
            threading.Thread(target=self._collect_in_background, daemon=True).start()

    def _refresh_data(self) -> None:
        # Показываем надпись о сборе информации ТОЛЬКО при самом первом запуске
        if self._is_first_run:
            self._status_label.config(text="Сбор информации о системе...")

        self._ensure_worker_running()

    def _collect_in_background(self) -> None:
        """Один бесконечный поток. Собирает данные раз в секунду без скачков в 0%."""
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
                    # Передаем флаг первого запуска в ваш collector
                    snapshot = self._collector.collect_all(is_first_run=self._is_first_run)
                    self._result_queue.put(snapshot)
                    
                    if self._is_first_run:
                        self._is_first_run = False
                        
                    # Поток засыпает ровно на 1 секунду между замерами
                    time.sleep(1.0)
                except Exception as exc:
                    logger.exception("Ошибка при сборе данных: %s", exc)
                    time.sleep(1.0)
        finally:
            if com_initialized:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def _poll_queue(self) -> None:
        """Просто забирает готовый snapshot и отдает его в ваш родной _populate_ui."""
        # Запускаем фоновый поток ТОЛЬКО ОДИН раз при старте приложения
        self._ensure_worker_running()

        # Выгребаем из очереди самый свежий секундный снимок железа
        snapshot = None
        while True:
            try:
                snapshot = self._result_queue.get_nowait()
            except queue.Empty:
                break
                
        # Если поток прислал новые данные, обновляем UI вашим стандартным методом
        if snapshot:
            self._latest_snapshot = snapshot
            self._populate_ui(snapshot)  # Родной метод гарантирует сохранность ваших названий параметров!
            self._status_label.config(text=f"Последнее обновление: {snapshot.timestamp}")

        # Проверяем очередь каждую 1 с
        self.after(1000, self._poll_queue)

    # ------------------------------------------------------------------ #
    # Заполнение виджетов данными снимка
    # ------------------------------------------------------------------ #
    def _populate_ui(self, snapshot: SystemSnapshot) -> None:
        self._populate_overview(snapshot)
        self._populate_kv(self._tabs["os"], self._os_rows(snapshot))
        self._populate_kv(self._tabs["cpu"], self._cpu_rows(snapshot))
        self._populate_kv(self._tabs["memory"], self._memory_rows(snapshot))
        self._populate_table(self._tabs["disk"], self._disk_rows(snapshot))
        self._populate_table(self._tabs["network"], self._network_rows(snapshot))

        # Видеокарту теперь тоже обновляем через умный метод на месте
        self._populate_table(self._tabs["gpu"], self._gpu_rows(snapshot))

        # Материнская плата автоматически обновляется бесшовно через _populate_kv
        self._populate_kv(self._tabs["motherboard"], self._motherboard_rows(snapshot))
        self._populate_kv(self._tabs["battery"], self._battery_rows(snapshot))

        # Диспетчер задач содержит сотни строк — перестраиваем таблицу только
        # когда пользователь на неё действительно смотрит, чтобы не тратить
        # время главного потока на невидимую вкладку каждую секунду.
        if self._is_process_tab_active():
            self._populate_process_tab(snapshot)

    @staticmethod
    def _update_tree_rows(tree: ttk.Treeview, new_rows: List[Tuple]) -> None:
        """
        Обновляет Treeview на месте:
        1. Если строка с таким ключом (первым столбцом) есть — обновляет её.
        2. Если нет — добавляет новую.
        3. Если строка исчезла из новых данных — удаляет её.
        """
        # Создаем карту существующих строк: {значение_первого_столбца: iid_строки}
        current_map = {tree.item(iid, "values")[0]: iid for iid in tree.get_children()}

        # Обрабатываем новые данные
        for row in new_rows:
            key = row[0]
            if key in current_map:
                # Обновляем существующую строку на месте
                tree.item(current_map[key], values=row)
                # Удаляем из карты, чтобы отметить как "обработанную"
                del current_map[key]
            else:
                # Добавляем новую
                tree.insert("", tk.END, values=row)

        # Удаляем те строки, которых больше нет в новых данных
        for iid in current_map.values():
            tree.delete(iid)

    def _populate_kv(self, tree: ttk.Treeview, rows: List[Tuple[str, str]]) -> None:
        # Преобразуем список кортежей в формат, подходящий для универсального метода
        self._update_tree_rows(tree, rows)

    def _populate_table(self, tree: ttk.Treeview, rows: List[Tuple]) -> None:
        # Универсальный метод уже умеет работать с таблицами
        self._update_tree_rows(tree, rows)

    def _populate_process_tab(self, s: SystemSnapshot) -> None:
        """Фильтрует, сортирует и отображает список процессов.

        ВАЖНО: значение PID кладём в таблицу как строку (``str(p.pid)``), а
        не как ``int``. Универсальный метод ``_update_tree_rows`` определяет
        "ту же самую" строку по совпадению первого столбца с тем, что уже
        хранится в Treeview, а Tk всегда возвращает значения ячеек строками.
        Если положить туда ``int``, сравнение никогда не совпадёт, и таблица
        будет каждую секунду удалять и заново создавать вообще все строки —
        а вместе с ними сбрасывать выделение пользователя.
        """
        processes = list(s.processes)

        query = self._process_filter_var.get().strip().lower()
        if query:
            processes = [p for p in processes if query in p.name.lower() or query == str(p.pid)]

        key_func = _PROCESS_SORT_KEYS.get(self._process_sort_column, _PROCESS_SORT_KEYS["cpu"])
        processes.sort(key=key_func, reverse=self._process_sort_reverse)

        self._process_row_data = {str(p.pid): p for p in processes}

        rows = [
            (
                str(p.pid),
                p.name,
                p.username,
                p.status,
                p.cpu_percent,
                p.memory_mb,
                p.memory_percent,
                p.disk_read_kb_s,
                p.disk_write_kb_s,
                p.network_connections,
                p.num_threads,
            )
            for p in processes
        ]
        self._update_tree_rows(self._tabs["processes"], rows)
        self._process_count_label.config(text=f"Процессов: {len(processes)}")

    def _refresh_process_view(self) -> None:
        """Перерисовывает вкладку «Диспетчер задач» из уже собранных данных
        (используется при вводе текста в поле поиска — без нового опроса ОС)."""
        if self._latest_snapshot is not None:
            self._populate_process_tab(self._latest_snapshot)

    def _sort_process_view(self, column: str) -> None:
        """Обрабатывает клик по заголовку столбца: сортирует по нему, повторный
        клик по тому же столбцу разворачивает порядок сортировки."""
        if self._process_sort_column == column:
            self._process_sort_reverse = not self._process_sort_reverse
        else:
            self._process_sort_column = column
            self._process_sort_reverse = column in _PROCESS_DESCENDING_BY_DEFAULT
        self._refresh_process_view()

    def _populate_overview(self, s: SystemSnapshot) -> None:
        rows = [
            ("Операционная система", f"{s.os_info.system} {s.os_info.release}"),
            ("Имя компьютера", s.os_info.node_name),
            ("Процессор", s.cpu_info.name),
            ("Физические ядра", str(s.cpu_info.physical_cores)),
            ("Логические ядра", str(s.cpu_info.logical_cores)),
            ("Установленная ОЗУ", f"{s.memory_info.total_gb} ГБ"),
            ("Материнская плата", f"{s.motherboard.manufacturer} {s.motherboard.product}"),
            ("Основная видеокарта", s.gpus[0].name if s.gpus else "Н/Д"),
            ("Время работы системы", s.os_info.uptime),
            ("Отчёт сформирован", s.timestamp),
        ]
        self._populate_kv(self._tabs["overview"], rows)

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

    def _cpu_rows(self, s: SystemSnapshot) -> List[Tuple[str, str]]:  # <-- ДОБАВИЛИ self
        c = s.cpu_info
        
        current_per_core = list(c.per_core_usage_percent)
        
        # ЗАЩИТА ОТ НУЛЯ: Теперь официально проверяем внутреннее хранилище через self
        is_all_zeros = all(v == 0.0 for v in current_per_core)
        
        if self._smoothed_cpu_data is not None:  # <-- Используем self вместо tk._default_root
            if is_all_zeros:
                current_per_core = self._smoothed_cpu_data
            else:
                # ПЛАВНЫЙ ПЕРЕХОД
                alpha = 0.4
                current_per_core = [
                    round(prev + alpha * (new - prev), 1)
                    for prev, new in zip(self._smoothed_cpu_data, current_per_core)
                ]
        
        # Сохраняем текущие значения в кэш класса через self
        self._smoothed_cpu_data = current_per_core

        # Пересчитываем общую загрузку на основе плавных ядер
        total_usage = round(sum(current_per_core) / len(current_per_core), 1) if current_per_core else 0.0

        rows = [
            ("Процессор (Модель)", c.name),
            ("Физические ядра", str(c.physical_cores)),
            ("Логические ядра", str(c.logical_cores)),
            ("Максимальная частота", f"{c.max_frequency_mhz} МГц"),
            ("Минимальная частота", f"{c.min_frequency_mhz} МГц"),
            ("Текущая частота", f"{c.current_frequency_mhz} МГц"),
            ("Общая загрузка CPU", f"{total_usage} %"),
        ]
        
        for idx, usage in enumerate(current_per_core):
            rows.append((f"  └─ Загрузка ядра {idx}", f"{usage} %"))
        return rows

    @staticmethod
    def _memory_rows(s: SystemSnapshot) -> List[Tuple[str, str]]:
        m = s.memory_info
        return [
            ("Всего ОЗУ", f"{m.total_gb} ГБ"),
            ("Доступно ОЗУ", f"{m.available_gb} ГБ"),
            ("Использовано ОЗУ", f"{m.used_gb} ГБ"),
            ("Использовано %", f"{m.used_percent} %"),
            ("Всего подкачки", f"{m.swap_total_gb} ГБ"),
            ("Использовано подкачки", f"{m.swap_used_gb} ГБ"),
            ("Свободно подкачки", f"{m.swap_free_gb} ГБ"),
            ("Использовано подкачки %", f"{m.swap_used_percent} %"),
        ]

    @staticmethod
    def _disk_rows(s: SystemSnapshot) -> List[Tuple]:
        return [
            (d.device, d.mountpoint, d.file_system, d.total_gb, d.used_gb, d.free_gb, f"{d.used_percent} %")
            for d in s.disks
        ]

    @staticmethod
    def _network_rows(s: SystemSnapshot) -> List[Tuple]:
        return [
            (n.name, n.ip_address, n.mac_address, n.bytes_sent_mb, n.bytes_recv_mb, "Активен" if n.is_up else "Отключен")
            for n in s.network_interfaces
        ]

    @staticmethod
    def _gpu_rows(s: SystemSnapshot) -> List[Tuple]:
        return [
            (
                f"{idx}. {g.name}", 
                g.driver_version, 
                f"{g.adapter_ram_gb} ГБ" if g.adapter_ram_gb > 0 else "Н/Д", 
                f"{g.used_ram_gb} ГБ",
                g.resolution
            ) 
            for idx, g in enumerate(s.gpus, start=1)
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

    @staticmethod
    def _battery_rows(s: SystemSnapshot) -> List[Tuple[str, str]]:
        b = s.battery
        if not b.present:
            return [("Статус", "Батарея не обнаружена (настольный компьютер)")]
        return [
            ("Заряд", f"{b.percent} %"),
            ("Питание от сети", "Да" if b.plugged_in else "Нет"),
            ("Осталось времени", b.time_left),
        ]

    # ------------------------------------------------------------------ #
    # Диспетчер задач: выбор строки, завершение процесса, окно свойств
    # ------------------------------------------------------------------ #
    def _get_selected_pid(self) -> Optional[int]:
        tree = self._tabs["processes"]
        selection = tree.selection()
        if not selection:
            return None
        values = tree.item(selection[0], "values")
        if not values:
            return None
        try:
            return int(values[0])
        except (ValueError, IndexError):
            return None

    def _show_process_context_menu(self, event: tk.Event) -> None:
        """Выделяет строку под курсором перед показом контекстного меню, чтобы
        правый клик работал так же, как в стандартном Диспетчере задач Windows —
        не требуя предварительного выделения левой кнопкой."""
        tree = self._tabs["processes"]
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

        # Завершение может занять до нескольких секунд (ожидание корректного
        # закрытия перед принудительным убийством) — уводим в фоновый поток,
        # чтобы интерфейс не "замирал".
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

        # Сбор подробных сведений делает несколько системных вызовов
        # (командная строка, открытые файлы и т.д.) — на всякий случай тоже
        # уводим в фоновый поток, чтобы не рисковать подвисанием интерфейса.
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

        win = tk.Toplevel(self)
        win.title(f"Свойства процесса — {details.name} (PID {pid})")
        win.geometry("500x540")
        win.transient(self)

        tree = ttk.Treeview(win, columns=("property", "value"), show="headings")
        tree.heading("property", text="Параметр")
        tree.heading("value", text="Значение")
        tree.column("property", width=200, anchor=tk.W)
        tree.column("value", width=280, anchor=tk.W)
        tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)

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

        button_bar = ttk.Frame(win, padding=(8, 0, 8, 8))
        button_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(
            button_bar,
            text="Завершить процесс",
            command=lambda: (win.destroy(), self._terminate_selected_process()),
        ).pack(side=tk.LEFT)
        ttk.Button(button_bar, text="Закрыть", command=win.destroy).pack(side=tk.RIGHT)

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
        lines = [f"Отчёт о системе — сформирован {s.timestamp}", "=" * 50]
        sections: List[Tuple[str, List[Tuple[str, str]]]] = [
            ("Операционная система", SystemInfoApp._os_rows(s)),
            ("Процессор", self._cpu_rows(s)), 
            ("Память", SystemInfoApp._memory_rows(s)),
            ("Материнская плата / BIOS", SystemInfoApp._motherboard_rows(s)),
            ("Батарея", SystemInfoApp._battery_rows(s)),
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
            lines.append(f"{g.name} (драйвер {g.driver_version}, видеопамять Всего: {g.adapter_ram_gb} ГБ / Использовано: {g.used_ram_gb} ГБ)")

        lines.append("\n[Топ-10 процессов по загрузке ЦП]")
        top_processes = sorted(s.processes, key=lambda p: p.cpu_percent, reverse=True)[:10]
        for p in top_processes:
            lines.append(
                f"PID {p.pid} — {p.name}: ЦП {p.cpu_percent}%, память {p.memory_mb} МБ ({p.memory_percent}%)"
            )

        return "\n".join(lines)

    def _show_about(self) -> None:
        messagebox.showinfo(
            APP_TITLE,
            f"{APP_TITLE}\n\n"
            "Лёгкая утилита для просмотра подробной информации об "
            "оборудовании и операционной системе, включая встроенный "
            "диспетчер задач (просмотр, завершение и свойства процессов).\n\n"
            "Создано с использованием Python, Tkinter, psutil и WMI.",
        )
