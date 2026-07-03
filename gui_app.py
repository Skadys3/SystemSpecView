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

from my_project.my_pet_project.collectors import SystemInfoCollector
from my_project.my_pet_project.models import SystemSnapshot

logger = logging.getLogger(__name__)

APP_TITLE = "Просмотр характеристик компьютера"
APP_MIN_WIDTH = 900
APP_MIN_HEIGHT = 600
LIVE_REFRESH_INTERVAL_MS = 1000
QUEUE_POLL_INTERVAL_MS = 150
CPU_SMOOTHING_FACTOR = 0.12


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

    # ------------------------------------------------------------------ #
    # Обновление данных: фоновый поток + потокобезопасная очередь
    # ------------------------------------------------------------------ #
    def _refresh_data(self) -> None:
        # Показываем надпись о сборе информации ТОЛЬКО при самом первом запуске
        if self._is_first_run:
            self._status_label.config(text="Сбор информации о системе...")
            
        threading.Thread(target=self._collect_in_background, daemon=True).start()

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
        if not getattr(self, "_worker_started", False):
            self._worker_started = True
            import threading
            t = threading.Thread(target=self._collect_in_background, daemon=True)
            t.start()

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

        return "\n".join(lines)

    def _show_about(self) -> None:
        messagebox.showinfo(
            APP_TITLE,
            f"{APP_TITLE}\n\n"
            "Лёгкая утилита для просмотра подробной информации об "
            "оборудовании и операционной системе.\n\n"
            "Создано с использованием Python, Tkinter, psutil и WMI.",
        )
