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
    timestamp: str
