"""
collectors.py

Слой сбора данных. Отвечает за получение сведений о системе, оборудовании
и конфигурации и возвращает их в виде типизированных объектов из
``models.py``. Слой графического интерфейса никогда не обращается к
psutil/wmi/platform напрямую — он взаимодействует только с
``SystemInfoCollector``.

Используются два источника:

* ``psutil`` / ``platform`` / ``socket`` (стандартные, кроссплатформенные)
  для получения информации о процессоре, памяти, дисках, сети, времени
  загрузки и батарее.
* ``wmi`` (Windows Management Instrumentation, только для Windows) для
  сведений, которые не предоставляет psutil: материнская плата, BIOS и
  идентификация видеокарты.

Если пакет ``wmi`` или его зависимость ``pywin32`` не установлены, либо
приложение запущено не на Windows, сборщик корректно деградирует:
поля, зависящие от WMI, просто принимают значение "Н/Д" по умолчанию,
вместо того чтобы вызывать исключение.
"""

# Пробуем подключить NVML (NVIDIA Management Library) — она позволяет
# получать "живые" данные об использованной видеопамяти без 32-битного
# переполнения, характерного для WMI. Доступна только при наличии
# видеокарты NVIDIA и установленных драйверов; если инициализация не
# удалась, просто отключаем NVML-путь и полагаемся на WMI/заглушки.
import warnings
try:
    from nvidia_ml import nvml as pynvml  # type: ignore
except ImportError:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="The pynvml package is deprecated")
        try:
            import pynvml  # type: ignore
        except ImportError:
            pynvml = None

_NVML_AVAILABLE = False
if pynvml is not None:
    try:
        pynvml.nvmlInit()
        _NVML_AVAILABLE = True
    except Exception:
        pass

import datetime
import logging
import platform
import socket
import time
from typing import Dict, List, Optional, Tuple

import psutil

from models import (
    BatteryInfo,
    CPUInfo,
    DiskPartitionInfo,
    GPUInfo,
    MemoryInfo,
    MotherboardInfo,
    NetworkInterfaceInfo,
    OSInfo,
    ProcessDetails,
    ProcessInfo,
    SystemSnapshot,
)

logger = logging.getLogger(__name__)

try:
    import wmi  # type: ignore

    _WMI_AVAILABLE = True
except ImportError:
    _WMI_AVAILABLE = False
    logger.warning(
        "Пакет 'wmi' недоступен: сведения о материнской плате, BIOS и "
        "видеокарте будут показаны как Н/Д. Установите его командой "
        "'pip install wmi pywin32' в Windows, чтобы включить эти данные."
    )


def _bytes_to_gb(value: int) -> float:
    """Преобразует количество байт в гигабайты, округляя до 2 знаков после запятой."""
    return round(value / (1024**3), 2)


def _bytes_to_mb(value: int) -> float:
    """Преобразует количество байт в мегабайты, округляя до 2 знаков после запятой."""
    return round(value / (1024**2), 2)


def _build_process_status_translation() -> Dict[str, str]:
    """Строит словарь перевода статусов процессов psutil на русский язык.

    Используем ``getattr`` с запасным значением, поскольку не все константы
    статусов существуют на всех платформах и во всех версиях psutil.
    """
    definitions = [
        ("STATUS_RUNNING", "Выполняется"),
        ("STATUS_SLEEPING", "Ожидание"),
        ("STATUS_DISK_SLEEP", "Ожидание диска"),
        ("STATUS_STOPPED", "Остановлен"),
        ("STATUS_TRACING_STOP", "Отладка"),
        ("STATUS_ZOMBIE", "Зомби"),
        ("STATUS_DEAD", "Завершён"),
        ("STATUS_WAKE_KILL", "Завершение"),
        ("STATUS_WAKING", "Пробуждение"),
        ("STATUS_IDLE", "Простой"),
        ("STATUS_LOCKED", "Заблокирован"),
        ("STATUS_WAITING", "Ожидание"),
        ("STATUS_SUSPENDED", "Приостановлен"),
        ("STATUS_PARKED", "Парковка"),
    ]
    translation: Dict[str, str] = {}
    for attr_name, russian_label in definitions:
        value = getattr(psutil, attr_name, None)
        if value is not None:
            translation[value] = russian_label
    return translation


_PROCESS_STATUS_RU = _build_process_status_translation()

# Приоритеты процессов Windows (Win32 priority classes). На других ОС psutil
# возвращает обычное значение "niceness", которое просто отображается как есть.
_WINDOWS_PRIORITY_RU = {
    "IDLE_PRIORITY_CLASS": "Низкий (фоновый)",
    "BELOW_NORMAL_PRIORITY_CLASS": "Ниже среднего",
    "NORMAL_PRIORITY_CLASS": "Обычный",
    "ABOVE_NORMAL_PRIORITY_CLASS": "Выше среднего",
    "HIGH_PRIORITY_CLASS": "Высокий",
    "REALTIME_PRIORITY_CLASS": "Реального времени",
}


class SystemInfoCollector:
    """Собирает информацию о системе, оборудовании и конфигурации по запросу."""

    def __init__(self) -> None:
        # Убираем жесткую инициализацию клиента из конструктора главного потока
        pass
        
        try:
            psutil.cpu_percent(percpu=True)
        except Exception:
            pass

        # Кэши для ускорения повторных опросов процессов
        self._username_cache: Dict[int, str] = {}
        self._create_cache: Dict[int, str] = {}
        self._process_io_poll_count = 0
        self._total_memory_bytes = 0

    def _get_wmi_client(self):
        """Возвращает локальный для текущего потока клиент WMI."""
        if _WMI_AVAILABLE:
            try:
                # Создаем экземпляр прямо в текущем (в т.ч. фоновом) потоке
                return wmi.WMI()
            except Exception as exc:
                logger.debug("Не удалось инициализировать клиент WMI в текущем потоке: %s", exc)
        return None

    # ------------------------------------------------------------------ #
    # Отдельные сборщики данных
    # ------------------------------------------------------------------ #
    def get_os_info(self) -> OSInfo:
        """Собирает идентификационные данные ОС, время загрузки и время работы."""
        try:
            boot_timestamp = psutil.boot_time()
            boot_dt = datetime.datetime.fromtimestamp(boot_timestamp)
            uptime_delta = datetime.datetime.now() - boot_dt
            return OSInfo(
                system=platform.system(),
                node_name=platform.node(),
                release=platform.release(),
                version=platform.version(),
                machine=platform.machine(),
                architecture=platform.architecture()[0],
                processor=platform.processor() or "Н/Д",
                boot_time=boot_dt.strftime("%Y-%m-%d %H:%M:%S"),
                uptime=str(uptime_delta).split(".")[0],
            )
        except Exception as exc:
            logger.error("Ошибка при сборе информации об ОС: %s", exc)
            return OSInfo()

    def get_cpu_info(self, is_first_run: bool = False) -> CPUInfo:
        """Собирает данные о CPU под структуру датакласса CPUInfo без просадок в 0%."""
        import psutil
        
        # На самом первом запуске принудительно делаем микрозамер (0.1 сек), чтобы убрать стартовый 0.0.
        # В последующие секунды ставим None: так как поток теперь бесконечный и работает через time.sleep(1.0),
        # psutil идеально считает разницу по системным тикам со времени прошлого вызова.
        interval = 0.1 if is_first_run else None
        
        total_pct = psutil.cpu_percent(interval=interval)
        cores_pct = psutil.cpu_percent(interval=interval, percpu=True)
        
        # Подстраховка: если из-за таймингов операционной системы None вернул чистый ноль на всех ядрах,
        # используем последнее известное рабочее значение (если оно сохранилось в кэше)
        if not is_first_run and total_pct == 0.0 and sum(cores_pct) == 0.0:
            if hasattr(self, "_last_valid_total") and hasattr(self, "_last_valid_cores"):
                total_pct = self._last_valid_total
                cores_pct = self._last_valid_cores
        else:
            # Сохраняем удачные ненулевые замеры в кэш
            self._last_valid_total = total_pct
            self._last_valid_cores = cores_pct

        # Частоты процессора
        freq = psutil.cpu_freq()
        current_f = freq.current if freq else 0.0
        min_f = freq.min if freq else 0.0
        max_f = freq.max if freq else 0.0
        
        # Кэш имени процессора
        if not hasattr(self, "_cached_cpu_name"):
            self._cached_cpu_name = "Н/Д"
            wmi_client = self._get_wmi_client()
            if wmi_client:
                try:
                    for cpu in wmi_client.Win32_Processor():
                        if getattr(cpu, "Name", None):
                            self._cached_cpu_name = cpu.Name.strip()
                            break
                except Exception:
                    pass
            if self._cached_cpu_name == "Н/Д":
                import platform
                self._cached_cpu_name = platform.processor() or "Unknown Processor"

        return CPUInfo(
            name=self._cached_cpu_name,
            physical_cores=psutil.cpu_count(logical=False) or 0,
            logical_cores=psutil.cpu_count(logical=True) or 0,
            max_frequency_mhz=max_f,
            min_frequency_mhz=min_f,
            current_frequency_mhz=current_f,
            total_usage_percent=total_pct,
            per_core_usage_percent=cores_pct
        )

    def get_memory_info(self) -> MemoryInfo:
        """Собирает данные об использовании физической ОЗУ и файла подкачки."""
        try:
            vm = psutil.virtual_memory()
            swap = psutil.swap_memory()
            return MemoryInfo(
                total_gb=_bytes_to_gb(vm.total),
                available_gb=_bytes_to_gb(vm.available),
                used_gb=_bytes_to_gb(vm.used),
                used_percent=vm.percent,
                swap_total_gb=_bytes_to_gb(swap.total),
                swap_used_gb=_bytes_to_gb(swap.used),
                swap_free_gb=_bytes_to_gb(swap.free),
                swap_used_percent=swap.percent,
            )
        except Exception as exc:
            logger.error("Ошибка при сборе информации о памяти: %s", exc)
            return MemoryInfo()

    def get_disk_info(self) -> List[DiskPartitionInfo]:
        """Собирает данные об использовании каждого подключённого несъёмного дискового раздела."""
        partitions_info: List[DiskPartitionInfo] = []
        try:
            for part in psutil.disk_partitions(all=False):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                except (PermissionError, OSError) as exc:
                    logger.debug("Пропуск раздела %s: %s", part.device, exc)
                    continue
                partitions_info.append(
                    DiskPartitionInfo(
                        device=part.device,
                        mountpoint=part.mountpoint,
                        file_system=part.fstype,
                        total_gb=_bytes_to_gb(usage.total),
                        used_gb=_bytes_to_gb(usage.used),
                        free_gb=_bytes_to_gb(usage.free),
                        used_percent=usage.percent,
                    )
                )
        except Exception as exc:
            logger.error("Ошибка при сборе информации о дисках: %s", exc)
        return partitions_info

    def get_network_info(self) -> List[NetworkInterfaceInfo]:
        """Собирает сетевые адреса и счётчики трафика для каждого сетевого адаптера."""
        interfaces: List[NetworkInterfaceInfo] = []
        try:
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            io_counters = psutil.net_io_counters(pernic=True)

            for name, addr_list in addrs.items():
                ip_address = "Н/Д"
                mac_address = "Н/Д"
                for addr in addr_list:
                    if addr.family == socket.AF_INET:
                        ip_address = addr.address
                    elif addr.family == socket.AF_INET6 and ip_address == "Н/Д":
                        ip_address = addr.address
                    elif addr.family == psutil.AF_LINK:
                        mac_address = addr.address

                is_up = stats[name].isup if name in stats else False
                io = io_counters.get(name)
                interfaces.append(
                    NetworkInterfaceInfo(
                        name=name,
                        ip_address=ip_address,
                        mac_address=mac_address,
                        bytes_sent_mb=_bytes_to_mb(io.bytes_sent) if io else 0.0,
                        bytes_recv_mb=_bytes_to_mb(io.bytes_recv) if io else 0.0,
                        is_up=is_up,
                    )
                )
        except Exception as exc:
            logger.error("Ошибка при сборе информации о сети: %s", exc)
        return interfaces

    def get_gpu_info(self, is_first_run: bool = False) -> List[GPUInfo]:
        """Собирает сведения о видеокарте (WMI только при первом запуске, NVML — всегда)."""
        gpus: List[GPUInfo] = []
        wmi_client = self._get_wmi_client()
        
        # Переменная для сохранения статических данных между вызовами внутри сборщика
        if not hasattr(self, "_static_gpus_cache"):
            self._static_gpus_cache = []

        # Вызываем тяжелый WMI ТОЛЬКО при первом запуске или если кэш пуст
        if is_first_run or not self._static_gpus_cache:
            if wmi_client:
                try:
                    for gpu in wmi_client.Win32_VideoController():
                        ram_bytes: Optional[int] = getattr(gpu, "AdapterRAM", None)
                        ram_gb = _bytes_to_gb(ram_bytes) if ram_bytes and ram_bytes > 0 else 0.0
                        h_res = getattr(gpu, "CurrentHorizontalResolution", None) or "?"
                        v_res = getattr(gpu, "CurrentVerticalResolution", None) or "?"
                        
                        self._static_gpus_cache.append({
                            "name": gpu.Name or "Н/Д",
                            "driver_version": gpu.DriverVersion or "Н/Д",
                            "adapter_ram_gb": ram_gb,
                            "resolution": f"{h_res}x{v_res}"
                        })
                except Exception as exc:
                    logger.error("Ошибка при сборе статической информации о GPU через WMI: %s", exc)
        
        # Если WMI ничего не вернул, создаем одну заглушку
        if not self._static_gpus_cache:
            self._static_gpus_cache = [{"name": "Н/Д", "driver_version": "Н/Д", "adapter_ram_gb": 0.0, "resolution": "Н/Д"}]

        # КАЖДУЮ СЕКУНДУ: собираем только живую память через быстрый NVML
        for idx, static_data in enumerate(self._static_gpus_cache):
            ram_gb = static_data["adapter_ram_gb"]
            used_gb = 0.0
            
            if _NVML_AVAILABLE:
                try:
                    handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
                    mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    
                    used_bytes = getattr(mem_info, "used", 0)
                    used_gb = round(float(used_bytes) / (1024**3), 2)
                    
                    if ram_gb == 0.0:
                        total_bytes = getattr(mem_info, "total", 0)
                        ram_gb = round(float(total_bytes) / (1024**3), 2)
                except Exception:
                    pass

            # ФИЛЬТРАЦИЯ: Если у видеокарты нет памяти (и WMI, и NVML вернули по нулям),
            # это фантомная запись дисплея. Пропускаем её, чтобы она не лезла в интерфейс.
            if ram_gb == 0.0 and used_gb == 0.0:
                continue

            gpus.append(
                GPUInfo(
                    name=static_data["name"],
                    driver_version=static_data["driver_version"],
                    adapter_ram_gb=ram_gb,
                    used_ram_gb=used_gb,
                    resolution=static_data["resolution"],
                )
            )
            
        # Страховка: если отфильтровали абсолютно всё, оставим одну чистую заглушку
        if not gpus:
            gpus.append(GPUInfo())
            
        return gpus

    def get_motherboard_info(self) -> MotherboardInfo:
        """Собирает сведения о материнской плате и прошивке BIOS/UEFI через WMI."""
        wmi_client = self._get_wmi_client()
        if wmi_client:
            try:
                board = wmi_client.Win32_BaseBoard()
                bios = wmi_client.Win32_BIOS()
                board_info = board[0] if board else None
                bios_info = bios[0] if bios else None
                return MotherboardInfo(
                    manufacturer=getattr(board_info, "Manufacturer", None) or "Н/Д",
                    product=getattr(board_info, "Product", None) or "Н/Д",
                    bios_vendor=getattr(bios_info, "Manufacturer", None) or "Н/Д",
                    bios_version=getattr(bios_info, "SMBIOSBIOSVersion", None) or "Н/Д",
                    serial_number=getattr(board_info, "SerialNumber", None) or "Н/Д",
                )
            except Exception as exc:
                logger.error("Ошибка при сборе информации о материнской плате через WMI: %s", exc)
        return MotherboardInfo()

    def get_battery_info(self) -> BatteryInfo:
        """Собирает состояние заряда батареи, если она присутствует."""
        try:
            battery = psutil.sensors_battery()
            if battery is None:
                return BatteryInfo(present=False)
            time_left = "Н/Д"
            if battery.secsleft not in (
                psutil.POWER_TIME_UNLIMITED,
                psutil.POWER_TIME_UNKNOWN,
            ):
                time_left = str(datetime.timedelta(seconds=battery.secsleft))
            return BatteryInfo(
                present=True,
                percent=battery.percent,
                plugged_in=battery.power_plugged,
                time_left=time_left,
            )
        except Exception as exc:
            logger.error("Ошибка при сборе информации о батарее: %s", exc)
            return BatteryInfo(present=False)

    # ------------------------------------------------------------------ #
    # Диспетчер задач: список процессов, свойства, завершение
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe(func, default):
        """Вызывает ``func()`` и возвращает ``default``, если это не удалось.

        Многие методы ``psutil.Process`` могут в любой момент выбросить
        ``AccessDenied`` (нет прав, обычно для системных процессов) или
        ``NoSuchProcess`` (процесс успел завершиться между вызовами) —
        такие поля просто мягко деградируют в "Н/Д", как и весь остальной
        сборщик данных в этом классе.
        """
        try:
            return func()
        except Exception:
            return default

    def _init_process_maps(self) -> None:
        """Лениво создаёт кэши, нужные для расчёта загрузки ЦП и скорости
        дискового ввода-вывода по процессам как разницы между двумя опросами
        (тот же приём, что уже используется для общей загрузки ЦП)."""
        if not hasattr(self, "_process_cache"):
            self._process_cache: Dict[int, "psutil.Process"] = {}
        if not hasattr(self, "_process_io_cache"):
            self._process_io_cache: Dict[int, Tuple[int, int]] = {}
        if not hasattr(self, "_process_last_poll_time"):
            self._process_last_poll_time: Optional[float] = None
        if not hasattr(self, "_process_conn_cache"):
            self._process_conn_cache: Dict[int, int] = {}
            self._process_conn_poll_count = 0

    def _get_connection_counts_by_pid(self) -> Dict[int, int]:
        """Возвращает словарь {pid: число активных сетевых подключений}.

        Кэширует результат на 30 вызовов, т.к. psutil.net_connections() —
        дорогой системный запрос (сканирует все TCP/UDP таблицы ядра).
        """
        self._init_process_maps()
        # Обновляем кэш раз в 30 вызовов (~30 секунд при опросе раз в сек)
        if self._process_conn_poll_count % 30 == 0:
            counts: Dict[int, int] = {}
            try:
                connections = psutil.net_connections(kind="inet")
            except Exception as exc:
                logger.debug("Не удалось получить список сетевых подключений: %s", exc)
                return self._process_conn_cache
            for conn in connections:
                if conn.pid:
                    counts[conn.pid] = counts.get(conn.pid, 0) + 1
            self._process_conn_cache = counts
        self._process_conn_poll_count += 1
        return self._process_conn_cache

    def get_process_list(self) -> List[ProcessInfo]:
        """Собирает построчный список всех запущенных процессов для вкладки
        «Диспетчер задач»: загрузка ЦП, память, скорость диска и сетевая
        активность.

        ОПТИМИЗАЦИИ (v3 — устранение лагов диспетчера задач):
        1. username() кэшируется на 30 секунд — это самый тяжёлый вызов на Windows
        2. create_time() кэшируется навсегда — никогда не меняется
        3. memory_percent() использует закэшированный total_memory, чтобы
           не вызывать psutil.virtual_memory() в каждой итерации
        4. io_counters() собирается только раз в 3 вызова (3 сек) — этого
           достаточно для плавного графика
        5. _process_io_poll_count тикает даже когда processes не собираются
        """
        self._init_process_maps()

        now = time.monotonic()
        elapsed = (now - self._process_last_poll_time) if self._process_last_poll_time else None
        self._process_last_poll_time = now

        connection_counts = self._get_connection_counts_by_pid()
        logical_cores = psutil.cpu_count(logical=True) or 1

        # Кэш общего объёма памяти для быстрого memory_percent без рекурсии
        if self._total_memory_bytes == 0:
            try:
                self._total_memory_bytes = psutil.virtual_memory().total
            except Exception:
                self._total_memory_bytes = 1

        # io_counters опрашиваем только раз в 3 секунды (каждый 3-й вызов)
        collect_io = (self._process_io_poll_count % 3) == 0
        self._process_io_poll_count += 1

        result: List[ProcessInfo] = []
        live_pids = set()

        for pid in psutil.pids():
            live_pids.add(pid)
            proc = self._process_cache.get(pid)
            is_new = proc is None
            if is_new:
                try:
                    proc = psutil.Process(pid)
                    proc.cpu_percent(None)
                    self._process_cache[pid] = proc
                except Exception:
                    continue

            try:
                with proc.oneshot():
                    name = proc.name() or "Н/Д"
                    raw_status = proc.status()
                    num_threads = proc.num_threads()
                    mem_info = proc.memory_info()
                    cpu_raw = 0.0 if is_new else proc.cpu_percent(None)

                # --- username: кэш на 30 секунд ---
                if pid in self._username_cache:
                    username = self._username_cache[pid]
                else:
                    username = self._safe(proc.username, "Н/Д")
                    self._username_cache[pid] = username

                # --- create_time: кэш навсегда ---
                if pid in self._create_cache:
                    create_str = self._create_cache[pid]
                else:
                    create_ts = self._safe(proc.create_time, None)
                    if create_ts:
                        create_str = datetime.datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        create_str = "Н/Д"
                    self._create_cache[pid] = create_str

                # --- memory_percent без вызова psutil.virtual_memory() в цикле ---
                mem_mb = _bytes_to_mb(mem_info.rss)
                mem_pct = round((mem_info.rss / self._total_memory_bytes) * 100.0, 1)

                # --- io_counters: раз в 3 секунды ---
                read_kb_s, write_kb_s = 0.0, 0.0
                if collect_io:
                    try:
                        io = proc.io_counters()
                        prev_io = self._process_io_cache.get(pid)
                        self._process_io_cache[pid] = (io.read_bytes, io.write_bytes)
                        if prev_io is not None and elapsed and elapsed > 0:
                            read_kb_s = round(max(0, io.read_bytes - prev_io[0]) / 1024 / elapsed, 1)
                            write_kb_s = round(max(0, io.write_bytes - prev_io[1]) / 1024 / elapsed, 1)
                    except Exception:
                        pass
                # В остальные секунды показываем 0 — данные всё равно не собранны

                cpu_pct = round(cpu_raw / logical_cores, 1)

                result.append(
                    ProcessInfo(
                        pid=pid,
                        name=name,
                        status=_PROCESS_STATUS_RU.get(raw_status, raw_status),
                        username=username,
                        cpu_percent=cpu_pct,
                        memory_mb=mem_mb,
                        memory_percent=mem_pct,
                        disk_read_kb_s=read_kb_s,
                        disk_write_kb_s=write_kb_s,
                        network_connections=connection_counts.get(pid, 0),
                        num_threads=num_threads,
                        create_time=create_str,
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._process_cache.pop(pid, None)
                self._process_io_cache.pop(pid, None)
                self._username_cache.pop(pid, None)
                self._create_cache.pop(pid, None)
                continue
            except Exception as exc:
                logger.debug("Пропуск процесса PID %s при сборе списка: %s", pid, exc)
                continue

        # Убираем из кэша процессы, которые уже завершились
        for old_pid in list(self._process_cache.keys()):
            if old_pid not in live_pids:
                del self._process_cache[old_pid]
                self._process_io_cache.pop(old_pid, None)
                self._username_cache.pop(old_pid, None)
                self._create_cache.pop(old_pid, None)

        # Раз в 30 вызовов чистим username_cache от мёртвых PID
        if self._process_io_poll_count % 30 == 0:
            for cached_pid in list(self._username_cache.keys()):
                if cached_pid not in live_pids:
                    self._username_cache.pop(cached_pid, None)
                    self._create_cache.pop(cached_pid, None)

        return result

    def _priority_to_str(self, nice_value) -> str:
        """Переводит числовое значение приоритета в понятную русскую подпись."""
        if nice_value is None:
            return "Н/Д"
        if platform.system() == "Windows":
            for attr_name, russian_label in _WINDOWS_PRIORITY_RU.items():
                if nice_value == getattr(psutil, attr_name, object()):
                    return russian_label
        return str(nice_value)

    def get_process_details(self, pid: int) -> Optional[ProcessDetails]:
        """Собирает расширенные сведения об одном процессе для окна «Свойства».

        В отличие от ``get_process_list``, вызывается по требованию — только
        когда пользователь открывает окно свойств конкретного процесса,
        поэтому не обязан быть таким же дешёвым, как построчный список.
        Возвращает ``None``, если процесс уже завершился.
        """
        try:
            proc = psutil.Process(pid)
            with proc.oneshot():
                name = proc.name() or "Н/Д"
                status = _PROCESS_STATUS_RU.get(proc.status(), proc.status())
                username = self._safe(proc.username, "Н/Д")
                exe_path = self._safe(proc.exe, "Н/Д") or "Н/Д"
                cmdline_list = self._safe(proc.cmdline, [])
                cmdline = " ".join(cmdline_list) if cmdline_list else "Н/Д"
                cwd = self._safe(proc.cwd, "Н/Д") or "Н/Д"
                create_ts = proc.create_time()
                priority = self._priority_to_str(self._safe(proc.nice, None))
                num_threads = proc.num_threads()
                cpu_times = proc.cpu_times()
                mem_info = proc.memory_info()
                mem_pct = round(proc.memory_percent(), 1)
                ppid = self._safe(proc.ppid, 0) or 0
                open_files = len(self._safe(proc.open_files, []))

            create_str = datetime.datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d %H:%M:%S")

            parent_name = "Н/Д"
            if ppid:
                try:
                    parent_name = psutil.Process(ppid).name()
                except Exception:
                    pass

            read_total_mb = write_total_mb = 0.0
            try:
                io = proc.io_counters()
                read_total_mb = _bytes_to_mb(io.read_bytes)
                write_total_mb = _bytes_to_mb(io.write_bytes)
            except Exception:
                pass

            connection_count = self._get_connection_counts_by_pid().get(pid, 0)

            return ProcessDetails(
                pid=pid,
                ppid=ppid,
                parent_name=parent_name,
                name=name,
                status=status,
                username=username,
                exe_path=exe_path,
                cmdline=cmdline,
                working_directory=cwd,
                create_time=create_str,
                priority=priority,
                num_threads=num_threads,
                cpu_user_time_s=round(cpu_times.user, 1),
                cpu_system_time_s=round(cpu_times.system, 1),
                memory_rss_mb=_bytes_to_mb(mem_info.rss),
                memory_vms_mb=_bytes_to_mb(mem_info.vms),
                memory_percent=mem_pct,
                disk_read_mb_total=read_total_mb,
                disk_write_mb_total=write_total_mb,
                open_files=open_files,
                network_connections=connection_count,
            )
        except psutil.NoSuchProcess:
            return None
        except Exception as exc:
            logger.error("Ошибка при сборе подробных сведений о процессе PID %s: %s", pid, exc)
            return None

    def terminate_process(self, pid: int, force: bool = False) -> Tuple[bool, str]:
        """Завершает процесс по PID.

        Без ``force`` сначала пытается закрыть процесс корректно
        (``terminate()`` — на Windows это ``TerminateProcess`` после попытки
        мягкого завершения через сообщения, у psutil — эквивалент SIGTERM),
        и только если он не завершился за отведённое время, "убивает" его
        принудительно. С ``force=True`` сразу переходит к принудительному
        завершению. Может вызываться из фонового потока: ``wait()`` внутри
        может занять до нескольких секунд, поэтому вызывающий код (GUI) не
        должен делать это в главном потоке интерфейса.

        Возвращает кортеж (успех, сообщение_для_пользователя_на_русском).
        """
        try:
            proc = psutil.Process(pid)
            name = proc.name()
        except psutil.NoSuchProcess:
            return False, f"Процесс с PID {pid} уже не существует."
        except psutil.AccessDenied:
            return False, f"Недостаточно прав для доступа к процессу с PID {pid}."

        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            return True, f"Процесс «{name}» (PID {pid}) успешно завершён."
        except psutil.NoSuchProcess:
            # Процесс успел завершиться сам, пока мы ждали — это тоже успех.
            return True, f"Процесс «{name}» (PID {pid}) успешно завершён."
        except psutil.AccessDenied:
            return False, (
                f"Недостаточно прав для завершения процесса «{name}» (PID {pid}). "
                "Попробуйте запустить приложение от имени администратора."
            )
        except Exception as exc:
            logger.error("Ошибка при завершении процесса PID %s: %s", pid, exc)
            return False, f"Не удалось завершить процесс «{name}» (PID {pid}): {exc}"

    def collect_all(self, is_first_run: bool = False, collect_processes: bool = True) -> SystemSnapshot:
        """Собирает все категории информации в единый снимок.

        ``collect_processes=False`` пропускает дорогой обход списка всех
        процессов системы (перечисление PID, запросы psutil по памяти,
        дисковому I/O и сетевым подключениям на КАЖДЫЙ процесс) и
        переиспользует последний собранный список. Используется, когда
        вкладка «Диспетчер задач» сейчас не видна пользователю — эти данные
        всё равно никто не смотрит, а их сбор на системе с несколькими
        сотнями процессов — самая дорогая операция во всём цикле опроса.
        """
        logger.debug("Сбор полного снимка состояния системы...")

        # Тяжёлые статические данные о материнской плате собираем только
        # ОДИН раз при старте. Раньше кэш определялся через истинность
        # выражения "_get_wmi_client() and get_motherboard_info()" — если
        # первая попытка получить клиент WMI случайно проваливалась,
        # результат (None) навсегда оставался "ложным", и WMI-запрос
        # повторялся заново на каждом цикле опроса. Явный флаг это исключает.
        if not hasattr(self, "_motherboard_collected"):
            self._cached_motherboard_info = (
                self.get_motherboard_info() if self._get_wmi_client() else MotherboardInfo()
            )
            self._motherboard_collected = True

        if collect_processes:
            self._last_process_list = self.get_process_list()
        elif not hasattr(self, "_last_process_list"):
            self._last_process_list = []

        snapshot = SystemSnapshot(
            os_info=self.get_os_info(),
            cpu_info=self.get_cpu_info(is_first_run=is_first_run),
            memory_info=self.get_memory_info(),  # Всегда собирается заново
            disks=self.get_disk_info(),          # Всегда собирается заново
            network_interfaces=self.get_network_info(), # Всегда собирается заново
            gpus=self.get_gpu_info(is_first_run=is_first_run),
            motherboard=self._cached_motherboard_info,
            battery=self.get_battery_info(),      # Всегда собирается заново
            processes=self._last_process_list,    # Для вкладки «Диспетчер задач»
            timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        logger.debug("Сбор снимка системы завершён.")
        return snapshot
