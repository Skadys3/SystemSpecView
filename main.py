"""
main.py

Точка входа в приложение «Просмотр характеристик компьютера».
"""

import logging
import platform
import sys
from pathlib import Path

if getattr(sys, 'frozen', False):
    EXE_DIR = Path(sys.executable).parent
    LOG_DIR = EXE_DIR / "logs"
else:
    LOG_DIR = Path(__file__).resolve().parent / "logs"

LOG_FILE = LOG_DIR / "system_specs_viewer.log"


def _configure_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _enable_windows_dpi_awareness() -> None:
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        logging.getLogger(__name__).debug("Не удалось установить DPI-осведомлённость.", exc_info=True)


def main() -> None:
    _configure_logging()
    logger = logging.getLogger(__name__)

    if platform.system() != "Windows":
        logger.warning(
            "Это приложение рассчитано на Windows. Некоторые аппаратные сведения "
            "(материнская плата, BIOS, видеокарта) будут показаны как Н/Д в %s, "
            "так как они зависят от Windows Management Instrumentation (WMI).",
            platform.system(),
        )

    _enable_windows_dpi_awareness()

    from gui_app import SystemInfoApp

    logger.info("Запуск приложения «Просмотр характеристик компьютера»...")
    app = SystemInfoApp()
    app.mainloop()
    logger.info("Приложение закрыто.")


if __name__ == "__main__":
    main()
