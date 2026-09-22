"""
main.py

Точка входа в приложение «Просмотр характеристик компьютера».
"""

import logging
import platform
import sys
from pathlib import Path

SUPPORTED_PLATFORMS = {"Windows", "Linux"}

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


def main() -> None:
    _configure_logging()
    logger = logging.getLogger(__name__)

    system = platform.system()

    if system not in SUPPORTED_PLATFORMS:
        logger.warning(
            "Платформа %s не имеет специализированного hardware provider. "
            "Часть аппаратных сведений может быть недоступна.",
            system,
        )


    from gui_app import SystemInfoApp

    logger.info("Запуск приложения «Просмотр характеристик компьютера»...")
    app = SystemInfoApp()
    app.mainloop()
    logger.info("Приложение закрыто.")


if __name__ == "__main__":
    main()
