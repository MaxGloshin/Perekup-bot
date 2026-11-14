"""Единая точка загрузки окружения: прод или локальная тестовая копия.

Зачем отдельный модуль. Раньше .env разбирали в двух местах (`load_dotenv()` в
bot.py и ещё раз в robokassa.py), и «какой файл подхватился» зависело от того,
из какой директории запустили процесс. Здесь это решается один раз и до любого
`os.getenv()` в остальных модулях: config импортируется первым — db.py, bot.py и
robokassa.py начинаются с него.

ПРАВИЛО ВЫБОРА ОКРУЖЕНИЯ — по наличию файла, без переключателей:

    есть .env.local  →  грузим ЕГО, APP_ENV = "local"
    нет .env.local   →  грузим .env,  APP_ENV = "production"

Файлы не смешиваются намеренно. `load_dotenv()` не перетирает уже выставленные
переменные, так что «сначала .env.local, потом .env» тихо подмешало бы боевые
значения для всего, что в .env.local не указано, — то есть локальный бот с
тестовым токеном ходил бы в боевую Robokassa. Либо один файл, либо другой.

На сервере .env.local просто нет, поэтому поведение прода не меняется ничем:
грузится тот же .env, DB_PATH по умолчанию тот же 'perekup.db'.
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта — рядом с этим файлом, а не текущая директория. load_dotenv()
# без аргумента ищет .env от cwd вверх, и запуск из другой папки (или из-под
# systemd с иным WorkingDirectory) нашёл бы не тот файл или не нашёл никакого.
BASE_DIR = Path(__file__).resolve().parent

# Уровень логов ставим здесь, а не только в main(): config импортируется раньше
# всех, а logging.basicConfig() срабатывает лишь ПЕРВЫМ вызовом. Любой
# logging.info()/warning() на уровне модуля (их два — про прокси Gemini и про
# заглушку) сам зовёт basicConfig() с уровнем по умолчанию WARNING, и повторный
# вызов из main() уже ничего не меняет — INFO-логи анализа тихо пропадали.
logging.basicConfig(level=logging.INFO)

ENV_LOCAL_FILE = BASE_DIR / ".env.local"
ENV_FILE = BASE_DIR / ".env"

if ENV_LOCAL_FILE.exists():
    APP_ENV = "local"
    ENV_FILE_USED = ENV_LOCAL_FILE
else:
    APP_ENV = "production"
    ENV_FILE_USED = ENV_FILE

load_dotenv(ENV_FILE_USED)

IS_LOCAL = APP_ENV == "local"

# Путь к файлу SQLite. Единственное место, откуда его берёт db.get_connection().
# Путь НЕ разворачивается в абсолютный: сейчас sqlite3.connect('perekup.db')
# считает его от рабочей директории процесса, и привязка к BASE_DIR молча
# увела бы прод на другой (пустой) файл, если у systemd-юнита WorkingDirectory
# указывает не сюда.
DB_PATH = (os.getenv("DB_PATH") or "").strip() or "perekup.db"

# Заглушка вместо реальных вызовов Gemini — см. mock_gemini.py.
# Значения: 1/true/yes — обычный правдоподобный ответ; nodata — сборщик цен
# возвращает пустоту (ветка «НЕТ ДАННЫХ» + дисклеймер); fail — сборщик падает
# с исключением (проверка возврата лимита и сообщения об ошибке).
MOCK_GEMINI_MODE = (os.getenv("MOCK_GEMINI") or "").strip().lower()
MOCK_GEMINI = MOCK_GEMINI_MODE in ("1", "true", "yes", "on", "nodata", "fail")


def describe() -> str:
    return (f"APP_ENV={APP_ENV} (файл {ENV_FILE_USED.name}) | DB_PATH={DB_PATH}"
            f" | MOCK_GEMINI={MOCK_GEMINI_MODE or 'off'}")


# Печатаем сразу на импорте, а не логируем: logging.basicConfig() вызывается
# в main() уже после того, как модули разобрали переменные, так что
# logging.info() отсюда никуда бы не попал. Видеть окружение нужно первой же
# строкой — иначе тестовый прогон против боевой базы замечаешь слишком поздно.
#
# В stderr, а не в stdout: у check_robokassa.py stdout — это результат (ссылка
# на оплату, которую копируют в браузер), и баннер в нём мешал бы. systemd
# пишет в журнал оба потока, так что на сервере строка всё равно видна.
print(f"[config] {describe()}", file=sys.stderr)
if IS_LOCAL:
    print("[config] ЛОКАЛЬНОЕ окружение: .env.local. Боевые данные не затрагиваются.",
          file=sys.stderr)


def log_startup():
    """То же самое в лог — вызывается из main() после basicConfig()."""
    logging.info("Окружение: %s", describe())
