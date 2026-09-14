"""Platform-specific hardware providers for SystemSpecView.

The main collector keeps the public ``SystemInfoCollector`` API stable while
this module isolates operating-system-specific hardware discovery.  The
providers are intentionally dependency-light: Linux uses sysfs/DMI and
optional command-line utilities, while Windows remains compatible with the
existing WMI implementation in ``collectors.py``.
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from models import GPUInfo, MotherboardInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlatformCapabilities:
    """Capabilities detected for the current operating system."""

    system: str
    dmi: bool = False
    hwmon: bool = False
    nvidia_smi: bool = False
    lspci: bool = False


class PlatformCollector:
    """Base interface for platform-specific hardware discovery."""

    capabilities = PlatformCapabilities(system=platform.system())

    def get_motherboard_info(self) -> Optional[MotherboardInfo]:
        return None

    def get_gpu_info(self) -> Optional[List[GPUInfo]]:
        return None


class WindowsPlatformCollector(PlatformCollector):
    """Marker provider for Windows.

    The existing WMI implementation remains in ``collectors.py`` for now so
    the current behaviour and public API are preserved during the migration.
    """

    capabilities = PlatformCapabilities(system="Windows")


class LinuxPlatformCollector(PlatformCollector):
    """Linux hardware provider using DMI, sysfs and optional utilities."""

    _DMI_ROOT = Path("/sys/class/dmi/id")
    _DRM_ROOT = Path("/sys/class/drm")

    def __init__(self) -> None:
        self.capabilities = PlatformCapabilities(
            system="Linux",
            dmi=self._DMI_ROOT.exists(),
            hwmon=Path("/sys/class/hwmon").exists(),
            nvidia_smi=shutil.which("nvidia-smi") is not None,
            lspci=shutil.which("lspci") is not None,
        )
        self._cached_gpus: Optional[List[GPUInfo]] = None

    @staticmethod
    def _read(path: Path) -> str:
        try:
            value = path.read_text(encoding="utf-8", errors="replace").strip()
            return value or "Н/Д"
        except (OSError, UnicodeError):
            return "Н/Д"

    @staticmethod
    def _run(command: List[str], timeout: float = 2.0) -> str:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            if result.returncode != 0:
                return ""
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    @staticmethod
    def _clean(value: str) -> str:
        value = value.strip().strip('"')
        return value if value else "Н/Д"

    def get_motherboard_info(self) -> Optional[MotherboardInfo]:
        if not self.capabilities.dmi:
            return MotherboardInfo()

        return MotherboardInfo(
            manufacturer=self._read(self._DMI_ROOT / "board_vendor"),
            product=self._read(self._DMI_ROOT / "board_name"),
            bios_vendor=self._read(self._DMI_ROOT / "bios_vendor"),
            bios_version=self._read(self._DMI_ROOT / "bios_version"),
            serial_number=self._read(self._DMI_ROOT / "board_serial"),
        )

    def _gpu_names_from_lspci(self) -> List[tuple[str, str]]:
        """Return ``[(name, pci_address), ...]`` for display controllers."""
        if not self.capabilities.lspci:
            return []

        output = self._run(["lspci", "-D", "-nn", "-k"])
        if not output:
            return []

        results: List[tuple[str, str]] = []
        current_address = ""
        current_is_gpu = False
        current_name = ""

        for line in output.splitlines():
            if line and not line.startswith((" ", "\t")):
                parts = line.split(" ", 1)
                current_address = parts[0]
                current_name = parts[1] if len(parts) > 1 else ""
                current_is_gpu = bool(re.search(r"(VGA compatible controller|3D controller|Display controller)", current_name, re.I))
                if current_is_gpu:
                    current_name = re.sub(r"\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]", "", current_name).strip()
                    results.append((current_name, current_address))

        return results

    @staticmethod
    def _driver_for_pci_address(address: str) -> str:
        device = Path("/sys/bus/pci/devices") / address / "driver"
        try:
            return device.resolve().name
        except OSError:
            return "Н/Д"

    def _nvidia_rows(self) -> dict[str, tuple[float, float, str]]:
        """Return NVIDIA VRAM data keyed by GPU name when nvidia-smi exists."""
        if not self.capabilities.nvidia_smi:
            return {}
        output = self._run([
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,driver_version",
            "--format=csv,noheader,nounits",
        ])
        rows: dict[str, tuple[float, float, str]] = {}
        for line in output.splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 4:
                continue
            try:
                total = round(float(fields[1]) / 1024.0, 2)
                used = round(float(fields[2]) / 1024.0, 2)
            except ValueError:
                continue
            rows[fields[0]] = (total, used, fields[3] or "Н/Д")
        return rows

    def get_gpu_info(self) -> Optional[List[GPUInfo]]:
        if self._cached_gpus is not None:
            return self._refresh_gpu_usage(self._cached_gpus)

        nvidia = self._nvidia_rows()
        discovered: List[GPUInfo] = []
        for name, pci_address in self._gpu_names_from_lspci():
            driver = self._driver_for_pci_address(pci_address)
            total, used, nvidia_driver = nvidia.get(name, (0.0, 0.0, "Н/Д"))
            if total == 0.0:
                for nvidia_name, values in nvidia.items():
                    if nvidia_name in name or name in nvidia_name:
                        total, used, nvidia_driver = values
                        break
            if nvidia_driver != "Н/Д":
                driver = nvidia_driver
            discovered.append(
                GPUInfo(
                    name=self._clean(name),
                    driver_version=driver,
                    adapter_ram_gb=total,
                    used_ram_gb=used,
                    resolution="Н/Д",
                )
            )

        if not discovered and nvidia:
            discovered = [
                GPUInfo(name=name, driver_version=values[2], adapter_ram_gb=values[0], used_ram_gb=values[1])
                for name, values in nvidia.items()
            ]

        self._cached_gpus = discovered or [GPUInfo()]
        return self._refresh_gpu_usage(self._cached_gpus)

    def _refresh_gpu_usage(self, gpus: List[GPUInfo]) -> List[GPUInfo]:
        nvidia = self._nvidia_rows()
        if not nvidia:
            return list(gpus)

        refreshed: List[GPUInfo] = []
        for gpu in gpus:
            match = nvidia.get(gpu.name)
            if match is None:
                for name, values in nvidia.items():
                    if name in gpu.name or gpu.name in name:
                        match = values
                        break
            if match is None:
                refreshed.append(gpu)
                continue
            total, used, driver = match
            refreshed.append(
                GPUInfo(
                    name=gpu.name,
                    driver_version=driver,
                    adapter_ram_gb=total,
                    used_ram_gb=used,
                    resolution=gpu.resolution,
                )
            )
        return refreshed


def create_platform_collector() -> PlatformCollector:
    """Create the provider appropriate for the running operating system."""
    system = platform.system()
    if system == "Linux":
        return LinuxPlatformCollector()
    if system == "Windows":
        return WindowsPlatformCollector()
    return PlatformCollector()
