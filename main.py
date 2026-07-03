"""
main.py

Точка входа в приложение «Просмотр характеристик компьютера».

Использование:
    python main.py

Зависимости:
    См. requirements.txt. В Windows установите их командой:
        pip install -r requirements.txt
"""

import logging
import platform
import sys
import os
from pathlib import Path

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.abspath(os.path.join(current_dir, "..", ".."))

# Принудительно добавляем её в пути поиска Python
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Проверяем, запущена ли программа как скомпилированный .exe
if getattr(sys, 'frozen', False):
    # Логи будут сохраняться в папку logs рядом с вашим .exe файлом
    EXE_DIR = Path(sys.executable).parent
    LOG_DIR = EXE_DIR / "logs"
else:
    # Обычный запуск .py скрипта в редакторе кода
    LOG_DIR = Path(__file__).resolve().parent / "logs"
    # Этот путь нужен только для разработки, чтобы Python видел пакеты
    sys.path.append(str(Path(__file__).resolve().parents[2]))

LOG_FILE = LOG_DIR / "system_specs_viewer.log"
sys.path.append(str(Path(__file__).resolve().parents[2]))


def _configure_logging() -> None:
    """Настраивает логирование приложения — одновременно в файл и в консоль."""
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
    """Делает интерфейс чётким на Windows-дисплеях с высоким DPI.

    Без этого окна Tkinter могут выглядеть размытыми на экранах с
    масштабированием, поскольку Windows растягивает всё изображение окна
    целиком, вместо того чтобы дать приложению отрисоваться в
    исходном разрешении.
    """
    if platform.system() != "Windows":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
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

    # Импортируется после настройки логирования, чтобы предупреждения,
    # возникающие при импорте (например, отсутствие пакета 'wmi'),
    # тоже попадали в лог-файл.
    from my_project.my_pet_project.gui_app import SystemInfoApp

    logger.info("Запуск приложения «Просмотр характеристик компьютера»...")
    app = SystemInfoApp()
    app.mainloop()
    logger.info("Приложение закрыто.")


if __name__ == "__main__":
    main()
