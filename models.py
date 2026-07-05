"""
models.py

Типизированные структуры данных, представляющие информацию о системе,
оборудовании и конфигурации, которую собирает ``collectors.SystemInfoCollector``.

Использование dataclass отделяет слой данных от слоя графического интерфейса:
GUI никогда не должен знать, *как* было получено значение — только то,
что его можно найти по чётко определённому имени атрибута.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class OSInfo:
    """Идентификация операционной системы и данные о времени работы."""

    system: str = "Н/Д"
    node_name: str = "Н/Д"
    release: str = "Н/Д"
    version: str = "Н/Д"
    machine: str = "Н/Д"
    architecture: str = "Н/Д"
    processor: str = "Н/Д"
    boot_time: str = "Н/Д"
    uptime: str = "Н/Д"


@dataclass
class CPUInfo:
    """Идентификация процессора и метрики загрузки."""

    name: str = "Н/Д"
    physical_cores: int = 0
    logical_cores: int = 0
    max_frequency_mhz: float = 0.0
    min_frequency_mhz: float = 0.0
    current_frequency_mhz: float = 0.0
    total_usage_percent: float = 0.0
    per_core_usage_percent: List[float] = field(default_factory=list)


@dataclass
class MemoryInfo:
    """Использование физической ОЗУ и файла подкачки."""

    total_gb: float = 0.0
    available_gb: float = 0.0
    used_gb: float = 0.0
    used_percent: float = 0.0
    swap_total_gb: float = 0.0
    swap_used_gb: float = 0.0
    swap_free_gb: float = 0.0
    swap_used_percent: float = 0.0


@dataclass
class DiskPartitionInfo:
    """Данные об использовании одного дискового раздела/тома."""

    device: str
    mountpoint: str
    file_system: str
    total_gb: float
    used_gb: float
    free_gb: float
    used_percent: float


@dataclass
class NetworkInterfaceInfo:
    """Сетевые адреса и счётчики трафика для одного сетевого адаптера."""

    name: str
    ip_address: str
    mac_address: str
    bytes_sent_mb: float
    bytes_recv_mb: float
    is_up: bool


@dataclass
class GPUInfo:
    """Идентификация видеокарты (графического адаптера)."""

    name: str = "Н/Д"
    driver_version: str = "Н/Д"
    adapter_ram_gb: float = 0.0
    used_ram_gb: float = 0.0
    resolution: str = "Н/Д"


@dataclass
class MotherboardInfo:
    """Сведения о материнской плате и прошивке BIOS/UEFI."""

    manufacturer: str = "Н/Д"
    product: str = "Н/Д"
    bios_vendor: str = "Н/Д"
    bios_version: str = "Н/Д"
    serial_number: str = "Н/Д"


@dataclass
class BatteryInfo:
    """Наличие и состояние заряда батареи (только для ноутбуков)."""

    present: bool = False
    percent: float = 0.0
    plugged_in: bool = False
    time_left: str = "Н/Д"


@dataclass
class ProcessInfo:
    """Сведения об одном запущенном процессе для вкладки «Диспетчер задач».

    Это «лёгкая» строка таблицы, обновляемая каждую секунду для всех
    процессов сразу. Более подробные и редко меняющиеся сведения об
    одном конкретном процессе живут в ``ProcessDetails``.
    """

    pid: int
    name: str = "Н/Д"
    status: str = "Н/Д"
    username: str = "Н/Д"
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    memory_percent: float = 0.0
    disk_read_kb_s: float = 0.0
    disk_write_kb_s: float = 0.0
    network_connections: int = 0
    num_threads: int = 0
    create_time: str = "Н/Д"


@dataclass
class ProcessDetails:
    """Расширенные сведения об одном процессе для окна «Свойства процесса».

    Собирается по требованию (при открытии окна свойств), а не каждую
    секунду для всех процессов, так как часть этих данных (командная
    строка, открытые файлы) дороже получать и почти никогда не меняется
    в течение жизни процесса.
    """

    pid: int
    ppid: int
    parent_name: str = "Н/Д"
    name: str = "Н/Д"
    status: str = "Н/Д"
    username: str = "Н/Д"
    exe_path: str = "Н/Д"
    cmdline: str = "Н/Д"
    working_directory: str = "Н/Д"
    create_time: str = "Н/Д"
    priority: str = "Н/Д"
    num_threads: int = 0
    cpu_user_time_s: float = 0.0
    cpu_system_time_s: float = 0.0
    memory_rss_mb: float = 0.0
    memory_vms_mb: float = 0.0
    memory_percent: float = 0.0
    disk_read_mb_total: float = 0.0
    disk_write_mb_total: float = 0.0
    open_files: int = 0
    network_connections: int = 0


@dataclass
class SystemSnapshot:
    """Единый снимок всей системной информации на конкретный момент времени."""

    os_info: OSInfo
    cpu_info: CPUInfo
    memory_info: MemoryInfo
    disks: List[DiskPartitionInfo]
    network_interfaces: List[NetworkInterfaceInfo]
    gpus: List[GPUInfo]
    motherboard: MotherboardInfo
    battery: BatteryInfo
    processes: List[ProcessInfo]
    timestamp: str
