import logging
import os
import sqlite3
from datetime import datetime, timedelta

import config

# Файл БД берётся из config (переменная DB_PATH), а не зашит здесь: локальный
# прогон под тестовым ботом должен писать в СВОЮ базу. По умолчанию значение то
# же самое, 'perekup.db', поэтому для прода ничего не меняется.
DB_NAME = config.DB_PATH

# Дневные лимиты старых тарифов. Единственное место, где они заданы: bot.py
# импортирует их отсюда, потому что раньше пороги жили в двух местах и разъезжались.
DAILY_LIMITS = {'base': 10, 'premium': 50}

# Сколько живут купленные запросы. Совпадает с обещанием в PRICES_TEXT и офертой.
REQUEST_TTL_DAYS = 30

# Дата сгорания "вечной" партии — реферальные запросы не сгорают.
#
# Почему дата-заглушка, а не NULL и не отдельный флаг. NULL потребовал бы снять
# NOT NULL с expires_at, а в SQLite это не ALTER TABLE, а полная перезаливка
# боевого журнала покупок; вдобавок NULL ядовит в сравнениях (`expires_at > ?`
# даёт false, и вечная партия выпала бы из баланса), а `ORDER BY expires_at ASC`
# ставит NULL ПЕРВЫМИ — вечные тратились бы раньше сгораемых, ровно наоборот
# нужного. Отдельная колонка-флаг заставила бы дописать условие в три запроса,
# и забыть один из них — вопрос времени.
#
# С датой схема не меняется вообще, а все операторы дают верный результат сами:
# '9999...' > now (партия в балансе), ORDER BY expires_at ASC уводит вечные в
# хвост (сгораемое тратится первым — жжём то, что портится). Это не заглушка вне
# домена колонки вроде '-' в user_city: "сгорает 31.12.9999" — буквально верное
# утверждение. Единственная протечка — MIN(expires_at) в purchased_summary(),
# и она закрыта там же, в SQL.
NEVER_EXPIRES = '9999-12-31T23:59:59'

# Безлимитная подписка — ВТОРОЙ товар рядом с пакетами, а не замена им.
# Срок хранится в users.unlimited_until обычной ISO-строкой datetime.isoformat(),
# как premium_until: сравнения идут строками, и формат обязан совпадать с тем,
# что пишет Python, иначе строковое сравнение '2026-08-24 23:00' с
# '2026-08-24T10:00' даст неверный ответ (пробел < 'T'). Поэтому дату считаем
# в Python и никогда не через SQL-функцию datetime().
UNLIMITED_DAYS = 30

# Вид заказа в orders.kind. Для 'unlimited' колонка qty хранит НЕ количество
# запросов, а количество дней подписки: покупается срок, а не штуки.
ORDER_KIND_REQUESTS = 'requests'
ORDER_KIND_UNLIMITED = 'unlimited'

# Реферальная программа. Начисления кладутся партиями в purchased_requests
# с source='referral_*' и сроком NEVER_EXPIRES.
REF_FIRST_BONUS = 5        # разово за первую оплаченную покупку приглашённого
REF_BONUS_MIN_QTY = 5      # ...но только если та покупка была от стольких запросов
REF_PERCENT = 20           # процент с каждой покупки приглашённого, целочисленно
REF_MAX_TOTAL = 200        # пожизненный потолок реферальных начислений на рефовода

# Порог на разовый бонус — единственная защита от реально прибыльного абуза.
# Минимальная покупка это 1 запрос за 30 руб., и без порога цикл "новый аккаунт →
# своя же ссылка → покупка за 30 руб." возвращал бы 5 бессрочных запросов
# розничной ценой 150 руб., то есть пятикратно. С порогом в 5 запросов (150 руб.)
# возврат падает примерно до 100%, и схема перестаёт быть бизнесом.
#
# Процентная часть в пороге не нуждается: 20% возврата запросами против 100%
# списания с карты (плюс комиссия и фискальный чек) — это скидка тому, кто и так
# платит, а не печатный станок.

# Метка источника трафика: диплинк t.me/<bot>?start=src_<тег>. 'organic' — человек
# пришёл сам, без метки, и это же значение играет роль признака "источник ещё не
# присвоен" в set_acquisition_source. Отдельного флага (по образцу city_asked)
# здесь не нужно: "спросили или нет" не бывает, метка либо приехала в диплинке,
# либо нет.
#
# Поле независимо от referred_by и не конфликтует с ним: пришедший по src_-ссылке
# может потом сам кого-то пригласить. Это два разных факта о пользователе, а не
# два состояния одного.
DEFAULT_ACQUISITION_SOURCE = 'organic'

# Режим анализа. Один и тот же сбор цен, но разный вердикт на втором шаге:
# 'reseller' — оценка под перепродажу (историческое и единственное до этого
# поведение), 'buyer' — оценка под покупку для себя, 'seller' — подготовка
# СВОЕЙ вещи к продаже (рекомендуемая цена размещения и советы по объявлению).
# Дефолт обязан оставаться 'reseller': колонка добавляется миграцией к живой
# базе, и все, кто уже пользуется ботом, должны получить ровно то же, что
# получали вчера, пока сами не переключатся через /mode.
#
# Добавление третьего режима миграции НЕ требует: колонка TEXT, значение просто
# становится допустимым в ANALYSIS_MODES. Кортеж — единственный список
# допустимых значений; всё остальное (кнопки, названия, промпты второго шага)
# разложено по словарям в bot.py с ключами отсюда, чтобы новый режим нельзя
# было потерять в двухветочном if.
#
# Отдельного флага "спрашивали ли про режим" (по образцу city_asked) нет
# намеренно: вопрос задаётся один раз в обработчике принятия дисклеймера, а он
# по построению срабатывает ровно один раз за всю жизнь пользователя.
ANALYSIS_MODES = ('reseller', 'buyer', 'seller')
DEFAULT_MODE = 'reseller'

# С какого номера начинать InvId, если последовательность ещё не заведена.
# Дев-копия и прод считают номера каждая от своей БД: без разных баз они
# выдали бы Robokassa один и тот же InvId, а это Result.Code=4 при опросе.
DEFAULT_INVOICE_ID_BASE = 1_000_000


def get_connection():
    # WAL: писатель больше не блокирует читателей на всю БД.
    # busy_timeout: ждём освобождения блокировки, а не падаем с "database is locked".
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            free_checks_left INTEGER DEFAULT 3,
            premium_until TEXT,
            tier TEXT DEFAULT 'free',
            checks_used_today INTEGER DEFAULT 0,
            last_check_date TEXT DEFAULT '',
            disclaimer_accepted INTEGER DEFAULT 0,
            user_city TEXT,
            city_asked INTEGER NOT NULL DEFAULT 0,
            referred_by INTEGER,
            acquisition_source TEXT NOT NULL DEFAULT 'organic',
            mode TEXT NOT NULL DEFAULT 'reseller',
            unlimited_until TEXT,
            unlimited_reminded_for TEXT
        )
    ''')

    # Миграция: добавляем колонку для старых пользователей, если её нет
    cursor.execute("PRAGMA table_info(users)")
    columns = [column[1] for column in cursor.fetchall()]

    if 'disclaimer_accepted' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN disclaimer_accepted INTEGER DEFAULT 0")

    # Регион пользователя: по нему сборщик ищет цены, если в объявлении регион
    # не указан явно. Две колонки, а не одна: city_asked отличает "ещё не
    # спрашивали" от "спросили, человек осознанно пропустил" — в обоих случаях
    # user_city IS NULL, и без флага бот переспрашивал бы при каждом /start.
    if 'user_city' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN user_city TEXT")

    if 'city_asked' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN city_asked INTEGER NOT NULL DEFAULT 0")

    # Кто пригласил. NULL — пришёл сам. Пишется ровно один раз, при первом /start
    # с диплинком, и больше не меняется (см. set_referrer).
    #
    # Внешнего ключа нет намеренно: в SQLite они по умолчанию выключены, а
    # delete_user() (самолечение /broadcast по TelegramForbiddenError) реально
    # удаляет строки — значит referred_by может указывать на исчезнувшего
    # пользователя, и это проверяется в коде, а не констрейнтом.
    if 'referred_by' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by)"
    )

    # Источник трафика для маркетинговой аналитики (/stats). Пишется один раз,
    # при первом /start с диплинком src_<тег> — см. set_acquisition_source.
    #
    # NOT NULL с непустым дефолтом для ADD COLUMN в SQLite законен (тот же приём,
    # что у city_asked), и старые строки читаются как 'organic' без отдельного
    # бэкфилла: дефолт применяется на чтении. Взамен GROUP BY в acquisition_stats()
    # не может получить NULL-корзину рядом с 'organic'.
    #
    # Индекса намеренно нет: горячих выборок по колонке не бывает, читает её
    # только админский /stats — там полный скан дешевле лишнего индекса на записи.
    if 'acquisition_source' not in columns:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN acquisition_source TEXT NOT NULL "
            f"DEFAULT '{DEFAULT_ACQUISITION_SOURCE}'"
        )

    # Режим анализа: 'reseller' (вердикт под перепродажу), 'buyer' (вердикт
    # под покупку для себя) или 'seller' (подготовка своей вещи к продаже).
    # Тот же приём, что у acquisition_source: NOT NULL с
    # непустым дефолтом законен для ADD COLUMN в SQLite, и старые строки читаются
    # как 'reseller' без отдельного бэкфилла — то есть миграция ничего не меняет
    # в поведении для тех, кто уже пользуется ботом.
    #
    # Индекса нет: колонка читается только по первичному ключу (свой user_id
    # перед анализом), выборок и группировок по ней не бывает.
    if 'mode' not in columns:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN mode TEXT NOT NULL "
            f"DEFAULT '{DEFAULT_MODE}'"
        )

    # Безлимитная подписка: до какого момента она действует. NULL — подписки
    # никогда не было, и это единственное значение "нет подписки": истёкшую
    # дату мы не обнуляем, потому что по ней видно, что человек когда-то платил
    # (и она же нужна напоминанию о продлении).
    #
    # Колонка НЕ заменяет tier/premium_until (старые Stars-подписки) и не
    # заменяет purchased_requests (пакеты). Три механики сосуществуют, порядок
    # проверки задан в decrease_limit(): подписка → тариф → пакеты → бесплатные.
    if 'unlimited_until' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN unlimited_until TEXT")

    # Отметка "за этот период продления напоминание уже отправлено". Хранит не
    # дату отправки, а САМО значение unlimited_until, для которого напоминание
    # ушло: тогда проверка "напоминали?" — это сравнение двух колонок, без
    # арифметики над датами. Продлил подписку → unlimited_until изменился →
    # отметка перестала совпадать → следующее напоминание разрешено само собой.
    if 'unlimited_reminded_for' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN unlimited_reminded_for TEXT")

    # Миграция: is_premium дублировал tier и никогда не читался — убираем.
    # Источник правды о подписке — tier + premium_until.
    if 'is_premium' in columns:
        cursor.execute("ALTER TABLE users DROP COLUMN is_premium")

    # Журнал обработанных платежей = ключ идемпотентности. Telegram переотправляет
    # апдейт, если бот не подтвердил offset (упал сразу после обработки), и без
    # этой таблицы повтор выдавал бы ещё 30 дней бесплатно.
    # Отдельной таблицей, а не колонкой в users: у пользователя со временем
    # несколько платежей, колонка хранила бы только последний.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS processed_payments (
            charge_id    TEXT PRIMARY KEY,
            user_id      INTEGER NOT NULL,
            tier         TEXT NOT NULL,
            amount       INTEGER NOT NULL,
            payload      TEXT,
            processed_at TEXT NOT NULL
        )
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_processed_payments_user ON processed_payments(user_id)"
    )

    # Заказы Robokassa. Строка создаётся в момент выдачи ссылки на оплату, то есть
    # ДО того, как человек заплатил: подпись ссылки считается по InvId, значит номер
    # нужно знать заранее.
    #
    # AUTOINCREMENT здесь обязателен, а не косметика. Без него SQLite переиспользует
    # max(rowid)+1, и после удаления последней строки мы второй раз отдали бы
    # Robokassa уже занятый InvId — а это либо отказ на оплате, либо Result.Code=4
    # ("две операции с одним InvoiceID") при опросе, то есть заказ, статус которого
    # невозможно достоверно узнать.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            invoice_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            qty        INTEGER NOT NULL,
            amount_rub INTEGER NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            checked_at TEXT,
            paid_at    TEXT,
            state_code INTEGER,
            op_key     TEXT,
            kind       TEXT NOT NULL DEFAULT 'requests'
        )
    ''')

    # Что именно куплено этим заказом: 'requests' (пакет запросов, историческое
    # и до появления подписки единственное значение) или 'unlimited' (безлимит
    # на срок). У заказов на подписку qty — это ДНИ, а не штуки; в orders одна
    # колонка количества, и заводить вторую ради одного вида товара незачем.
    #
    # NOT NULL с непустым дефолтом законен для ADD COLUMN в SQLite — тот же
    # приём, что у mode и acquisition_source: все существующие заказы читаются
    # как 'requests' без бэкфилла, то есть миграция ничего не меняет.
    cursor.execute("PRAGMA table_info(orders)")
    order_columns = [column[1] for column in cursor.fetchall()]
    if 'kind' not in order_columns:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN kind TEXT NOT NULL "
            f"DEFAULT '{ORDER_KIND_REQUESTS}'"
        )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_pending ON orders(status, created_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)"
    )

    # Купленные запросы — журнал партий, а не колонка-баланс в users: запросы
    # сгорают через 30 дней с момента КАЖДОЙ покупки, и одна колонка не смогла бы
    # хранить разные даты сгорания. Баланс = SUM(qty - used) по непросроченным.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS purchased_requests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            qty        INTEGER NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT NOT NULL,
            source     TEXT NOT NULL,
            invoice_id INTEGER,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_purchased_user ON purchased_requests(user_id, expires_at)"
    )

    _seed_invoice_sequence(cursor)

    conn.commit()
    conn.close()


def _seed_invoice_sequence(cursor):
    """Сдвигает начало нумерации заказов на INVOICE_ID_BASE.

    Переменную читаем здесь, при первом init_db(), а не на импорте — так
    значение берётся из того окружения, которое загрузил config, и переопределить
    его в тесте можно обычным os.environ.

    sqlite_sequence создаётся самим SQLite вместе с таблицей с AUTOINCREMENT,
    и в неё можно писать напрямую. Строка появляется только после первой вставки,
    поэтому пустая последовательность — это "заказов ещё не было", её и сеем.
    """
    try:
        base = int(os.getenv("INVOICE_ID_BASE", DEFAULT_INVOICE_ID_BASE))
    except ValueError:
        base = DEFAULT_INVOICE_ID_BASE

    cursor.execute("SELECT seq FROM sqlite_sequence WHERE name = 'orders'")
    if cursor.fetchone() is None:
        cursor.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES ('orders', ?)",
            (base - 1,))


# Эти функции необходимы, чтобы bot.py не выдавал ошибку при работе с плашкой:
def get_disclaimer_status(user_id: int) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT disclaimer_accepted FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def accept_disclaimer(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET disclaimer_accepted = 1 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def get_city_state(user_id: int):
    """(город или None, спрашивали ли уже) — одним запросом, вызывается на каждом анализе."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT user_city, city_asked FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None, False
    return row[0], bool(row[1])


def set_city(user_id: int, city: str):
    """Сохраняет город и помечает вопрос закрытым."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE users SET user_city = ?, city_asked = 1 WHERE user_id = ?',
        (city, user_id))
    conn.commit()
    conn.close()


def skip_city(user_id: int):
    """Пользователь осознанно пропустил вопрос: города нет, но переспрашивать не надо."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE users SET user_city = NULL, city_asked = 1 WHERE user_id = ?',
        (user_id,))
    conn.commit()
    conn.close()


def get_mode(user_id: int) -> str:
    """Режим анализа пользователя: 'reseller' | 'buyer' | 'seller'.

    Незнакомый пользователь и мусор в колонке дают DEFAULT_MODE — вердикт
    обязан построиться в любом случае, а «неизвестный режим» это не ошибка,
    из-за которой стоит ронять анализ.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT mode FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row or row[0] not in ANALYSIS_MODES:
        return DEFAULT_MODE
    return row[0]


def set_mode(user_id: int, mode: str) -> bool:
    """Сохраняет режим анализа. False — режим неизвестен, в БД ничего не ушло."""
    if mode not in ANALYSIS_MODES:
        return False

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET mode = ? WHERE user_id = ?', (mode, user_id))
    conn.commit()
    conn.close()
    return True


def add_user(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, free_checks_left) VALUES (?, 3)', (user_id,))
    conn.commit()
    conn.close()

def user_exists(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT 1 FROM users WHERE user_id = ?",
        (user_id,)
    )

    exists = cursor.fetchone() is not None

    conn.close()
    return exists

def get_user(user_id: int):
    """Возвращает: (free_checks, tier, premium_until_str, checks_used, today_str)"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT free_checks_left, tier, premium_until, checks_used_today, last_check_date FROM users WHERE user_id = ?',
        (user_id,))
    row = cursor.fetchone()

    today_str = datetime.now().strftime("%Y-%m-%d")


    if not row:
        # Закрываем ДО add_user: он открывает своё соединение, и держать оба
        # одновременно незачем. Раньше этой строки не было — соединение утекало
        # на каждом обращении незарегистрированного пользователя.
        conn.close()
        add_user(user_id)
        return (3, 'free', None, 0, today_str)

    free_checks, tier, premium_until_str, checks_used, last_date = row

    # 1. Проверяем, не истекла ли подписка
    if tier != 'free' and premium_until_str:
        try:
            until_date = datetime.fromisoformat(premium_until_str)
            if datetime.now() > until_date:
                revoke_premium(user_id)
                tier = 'free'
                premium_until_str = None
        except ValueError:
            pass

    # 2. Проверяем, наступил ли новый день (сброс дневного лимита)
    if last_date != today_str:
        checks_used = 0  # Сбрасываем попытки, если дата сменилась

    conn.close()
    return (free_checks, tier, premium_until_str, checks_used, today_str)


def _spend_daily(user_id: int, tier: str) -> bool:
    """Занимает слот дневного лимита тарифа. True — заняли, False — на сегодня всё.

    Условие лимита стоит в WHERE, а не в отдельном чтении: иначе два одновременных
    фото прочитали бы один и тот же счётчик и оба сочли бы, что место есть.
    Смена даты выражена внутри UPDATE по той же причине — читать нечего.
    """
    limit = DAILY_LIMITS.get(tier)
    if limit is None:
        return False

    today_str = datetime.now().strftime("%Y-%m-%d")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users
        SET checks_used_today = CASE
                WHEN last_check_date = ? THEN checks_used_today + 1
                ELSE 1
            END,
            last_check_date = ?
        WHERE user_id = ?
          AND (last_check_date <> ? OR checks_used_today < ?)
    ''', (today_str, today_str, user_id, today_str, limit))
    taken = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return taken


def _spend_purchased(user_id: int) -> int | None:
    """Списывает один купленный запрос. Возвращает id партии или None.

    Тратим партию, которая сгорит раньше остальных. `used < qty` продублировано
    в WHERE самого UPDATE: между выбором партии и списанием её мог опустошить
    параллельный запрос, тогда rowcount = 0 и мы просто берём следующую.
    """
    now_iso = datetime.now().isoformat()

    conn = get_connection()
    cursor = conn.cursor()
    try:
        for _ in range(3):
            cursor.execute('''
                SELECT id FROM purchased_requests
                WHERE user_id = ? AND used < qty AND expires_at > ?
                ORDER BY expires_at ASC, id ASC
                LIMIT 1
            ''', (user_id, now_iso))
            row = cursor.fetchone()
            if not row:
                return None

            lot_id = row[0]
            cursor.execute(
                'UPDATE purchased_requests SET used = used + 1 WHERE id = ? AND used < qty',
                (lot_id,))
            if cursor.rowcount > 0:
                conn.commit()
                return lot_id
            conn.commit()
        return None
    finally:
        conn.close()


def _spend_free(user_id: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users
        SET free_checks_left = free_checks_left - 1
        WHERE user_id = ? AND free_checks_left > 0
    ''', (user_id,))
    taken = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return taken


def decrease_limit(user_id: int) -> str:
    """Списывает одну проверку и возвращает токен источника: 'sub', 'unlimited',
    'daily', 'lot:<id>', 'free' или 'none' (списывать было нечего).

    Порядок: безлимитная подписка → активный тариф → купленные запросы →
    бесплатные. Подписка стоит первой и не списывает ничего: пока она жива,
    пакетные запросы обязаны остаться нетронутыми на балансе (человек заплатил
    и за них тоже, и они сгорают по своему сроку независимо от подписки).
    Купленные тратятся раньше бесплатных, потому что сгорают через 30 дней,
    а бесплатные не сгорают никогда — жечь надо то, что портится.

    Токен нужен возврату: без него refund_limit не знает, куда класть проверку
    обратно, и мог бы вернуть купленную как бесплатную. Все ветки — атомарные
    UPDATE, ни одна не читает счётчик перед записью.

    Истёкшую подписку по-прежнему разжалует get_user() — все вызывающие
    проверяют лимит через него до списания.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT tier, unlimited_until FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return 'none'

    tier, unlimited_until = row

    if _unlimited_is_active(unlimited_until):
        return 'sub'  # подписка живая — не списываем вообще ничего

    if tier == 'unlimited':
        return 'unlimited'  # Безлимит не списываем

    if tier in DAILY_LIMITS and _spend_daily(user_id, tier):
        return 'daily'

    lot_id = _spend_purchased(user_id)
    if lot_id is not None:
        return f'lot:{lot_id}'

    if _spend_free(user_id):
        return 'free'

    return 'none'


def refund_limit(user_id: int, token: str | None = None):
    """Возвращает зарезервированную проверку, если анализ не дошёл до пользователя.

    token — то, что вернул decrease_limit. Без него источник списания
    восстановить нельзя: у пользователя одновременно могут быть и тариф,
    и купленные запросы, и бесплатные.
    """
    if token in ('sub', 'unlimited', 'none'):
        return  # Ничего не списывалось — возвращать нечего

    if token and token.startswith('lot:'):
        try:
            lot_id = int(token.split(':', 1)[1])
        except ValueError:
            return
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE purchased_requests SET used = MAX(used - 1, 0) WHERE id = ?',
            (lot_id,))
        conn.commit()
        conn.close()
        return

    if token is None:
        # Старый вызов без токена: восстанавливаем по тарифу, как было раньше.
        # Купленные так не вернуть, поэтому все живые вызовы токен передают.
        free_checks, tier, _, _, _ = get_user(user_id)
        token = 'daily' if tier in DAILY_LIMITS else 'free'

    conn = get_connection()
    cursor = conn.cursor()

    if token == 'daily':
        # MAX(..., 0) страхует от ухода в минус, если день сменился между списанием и возвратом
        cursor.execute('''
            UPDATE users
            SET checks_used_today = MAX(checks_used_today - 1, 0)
            WHERE user_id = ?
        ''', (user_id,))
    else:
        cursor.execute('''
            UPDATE users
            SET free_checks_left = free_checks_left + 1
            WHERE user_id = ?
        ''', (user_id,))

    conn.commit()
    conn.close()


def set_tier(user_id: int, new_tier: str, days: int = 30) -> datetime:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT premium_until, tier FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    now = datetime.now()

    # Если продлевает ТЕКУЩИЙ тариф — суммируем дни. Если меняет (апгрейд) — дни перезаписываются.
    if row and row[0] and row[1] == new_tier:
        try:
            current_until = datetime.fromisoformat(row[0])
            if current_until > now:
                new_until = current_until + timedelta(days=days)
            else:
                new_until = now + timedelta(days=days)
        except ValueError:
            new_until = now + timedelta(days=days)
    else:
        new_until = now + timedelta(days=days)

    cursor.execute('''
        UPDATE users
        SET tier = ?, premium_until = ?, checks_used_today = 0, last_check_date = ?
        WHERE user_id = ?
    ''', (new_tier, new_until.isoformat(), now.strftime("%Y-%m-%d"), user_id))

    conn.commit()
    conn.close()
    return new_until

def payment_already_processed(charge_id: str) -> bool:
    """Был ли этот платёж уже зачислен. Пустой charge_id дедуплицировать нечем."""
    if not charge_id:
        return False

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM processed_payments WHERE charge_id = ?', (charge_id,))
    found = cursor.fetchone() is not None
    conn.close()
    return found


def record_payment(charge_id: str, user_id: int, tier: str, amount: int, payload: str) -> bool:
    """Записывает платёж в журнал. True — записали, False — такой charge_id уже был.

    INSERT OR IGNORE поверх PRIMARY KEY: вставка атомарна, поэтому возвращённый
    признак различает первую обработку и дубль без отдельной блокировки.
    """
    if not charge_id:
        return False

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO processed_payments
            (charge_id, user_id, tier, amount, payload, processed_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (charge_id, user_id, tier, amount, payload, datetime.now().isoformat()))
    inserted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return inserted


def get_payment(charge_id: str):
    """(user_id, tier, amount, payload, processed_at) или None."""
    if not charge_id:
        return None

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT user_id, tier, amount, payload, processed_at '
        'FROM processed_payments WHERE charge_id = ?',
        (charge_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_all_users():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT user_id FROM users")
    users = [row[0] for row in cur.fetchall()]

    conn.close()
    return users

def get_users_by_tier(tier):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT user_id
        FROM users
        WHERE tier=?
        """,
        (tier,)
    )

    users = [row[0] for row in cur.fetchall()]

    conn.close()
    return users

def get_free_users():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT user_id
        FROM users
        WHERE tier='free'
        """
    )

    users = [row[0] for row in cur.fetchall()]

    conn.close()
    return users

def delete_user(user_id):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM users WHERE user_id=?",
        (user_id,)
    )

    conn.commit()
    conn.close()

def revoke_premium(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET tier = 'free', premium_until = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ==========================================
# ♾ БЕЗЛИМИТНАЯ ПОДПИСКА
# ==========================================

def _parse_dt(value: str | None):
    """ISO-строка в datetime или None. Битую дату считаем отсутствующей."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _unlimited_is_active(value: str | None, now: datetime | None = None) -> bool:
    """Живая ли подписка по строке unlimited_until.

    Сравниваем datetime, а не строки: истёкшая подписка не разжалуется записью
    в БД (в отличие от tier, который чистит revoke_premium), поэтому решение
    принимается на чтении и обязано быть одинаковым во всех точках.
    """
    until = _parse_dt(value)
    return until is not None and (now or datetime.now()) < until


def get_unlimited_until(user_id: int) -> str | None:
    """Сырое значение unlimited_until — включая уже истёкшее.

    Истёкшую дату не обнуляем: по ней видно, что человек когда-то платил, и
    её же читает экран продления.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT unlimited_until FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def unlimited_active_until(user_id: int):
    """datetime окончания ДЕЙСТВУЮЩЕЙ подписки или None, если её нет."""
    raw = get_unlimited_until(user_id)
    return _parse_dt(raw) if _unlimited_is_active(raw) else None


def _extend_unlimited(cursor, user_id: int, days: int, now: datetime) -> str:
    """Продлевает подписку на days дней курсором вызывающего. Новая дата (ISO).

    Отсчёт идёт от БОЛЬШЕЙ из двух дат — текущего момента и уже оплаченного
    срока: повторная покупка обязана добавлять дни к остатку, а не обнулять его
    до "30 дней от сейчас", иначе человек, заплативший заранее, теряет остаток.

    Читает и пишет одним курсором внутри транзакции вызывающего — так же, как
    _accrue_referral. Атомарность даёт не один оператор, а write-lock: к моменту
    вызова транзакция уже сделала запись (UPDATE orders в credit_order или
    BEGIN IMMEDIATE в extend_unlimited), а в WAL писатели сериализуются, значит
    двух одновременных продлений одного пользователя быть не может.
    """
    cursor.execute('SELECT unlimited_until FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    current = _parse_dt(row[0]) if row else None

    base = current if (current is not None and current > now) else now
    until = base + timedelta(days=days)
    until_iso = until.isoformat()

    cursor.execute(
        'UPDATE users SET unlimited_until = ? WHERE user_id = ?',
        (until_iso, user_id))
    return until_iso


def extend_unlimited(user_id: int, days: int = UNLIMITED_DAYS) -> str:
    """Продление подписки отдельной транзакцией — для ручной выдачи (/give sub).

    BEGIN IMMEDIATE берёт write-lock сразу, до чтения: иначе между SELECT и
    UPDATE успел бы вклиниться поллер с оплаченным заказом, и одно из двух
    продлений потерялось бы. Оплаченные заказы ходят другим путём, через
    credit_order, где транзакция уже открыта.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('BEGIN IMMEDIATE')
        until_iso = _extend_unlimited(cursor, user_id, days, datetime.now())
        conn.commit()
    finally:
        conn.close()
    return until_iso


def users_to_remind_unlimited(within_days: int = 2):
    """Кому пора напомнить о продлении: [(user_id, unlimited_until), ...].

    Условие ровно одно и целиком в SQL: подписка ещё жива, но истекает в
    ближайшие within_days, и напоминание именно за этот оплаченный период
    ещё не отправлялось (unlimited_reminded_for хранит то самое значение
    unlimited_until, для которого напоминание ушло).

    Сравнение строковое и потому корректное: обе даты пишутся одним и тем же
    datetime.isoformat(), формат посимвольно одинаков — см. UNLIMITED_DAYS.
    """
    now = datetime.now()
    horizon = (now + timedelta(days=within_days)).isoformat()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT user_id, unlimited_until FROM users
        WHERE unlimited_until IS NOT NULL
          AND unlimited_until > ?
          AND unlimited_until <= ?
          AND (unlimited_reminded_for IS NULL OR unlimited_reminded_for <> unlimited_until)
        ORDER BY unlimited_until ASC
    ''', (now.isoformat(), horizon))
    rows = cursor.fetchall()
    conn.close()
    return rows


def claim_unlimited_reminder(user_id: int, until_iso: str) -> bool:
    """Занимает право отправить напоминание. True — занял, отправляй.

    Проверка "не отправляли ли уже" и отметка — ОДИН оператор, по тому же
    принципу, что _insert_purchased_once: иначе два тика (или тик и перезапуск)
    оба прочитали бы пустую отметку и человек получил бы два одинаковых
    сообщения. Условие unlimited_until = ? страхует от гонки с продлением:
    если срок успел измениться, отметка не встанет и напоминание не уйдёт —
    период уже другой, а по нему напомним отдельно.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users SET unlimited_reminded_for = unlimited_until
        WHERE user_id = ?
          AND unlimited_until = ?
          AND (unlimited_reminded_for IS NULL OR unlimited_reminded_for <> unlimited_until)
    ''', (user_id, until_iso))
    claimed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return claimed


def active_unlimited_count() -> int:
    """Сколько живых подписок сейчас — для админской статистики."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT COUNT(*) FROM users WHERE unlimited_until IS NOT NULL AND unlimited_until > ?',
        (datetime.now().isoformat(),))
    count = cursor.fetchone()[0]
    conn.close()
    return count


# ==========================================
# 💳 ЗАКАЗЫ ROBOKASSA И КУПЛЕННЫЕ ЗАПРОСЫ
# ==========================================

_ORDER_FIELDS = ('invoice_id', 'user_id', 'qty', 'amount_rub', 'status',
                 'created_at', 'checked_at', 'paid_at', 'state_code', 'op_key',
                 'kind')


def _order_row(row):
    return dict(zip(_ORDER_FIELDS, row)) if row else None


def create_order(user_id: int, qty: int, amount_rub: int,
                 kind: str = ORDER_KIND_REQUESTS) -> int:
    """Заводит заказ и возвращает InvId — он же номер, который уйдёт в Robokassa.

    Строка появляется до оплаты: подпись ссылки считается по InvId, значит номер
    нужен раньше, чем человек нажмёт "оплатить".

    kind='unlimited' — заказ на безлимитную подписку, и тогда qty означает ДНИ
    подписки, а не количество запросов (см. ORDER_KIND_UNLIMITED).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO orders (user_id, qty, amount_rub, status, created_at, kind)
        VALUES (?, ?, ?, 'pending', ?, ?)
    ''', (user_id, qty, amount_rub, datetime.now().isoformat(), kind))
    invoice_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return invoice_id


def get_order(invoice_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(_ORDER_FIELDS)} FROM orders WHERE invoice_id = ?",
        (invoice_id,))
    row = cursor.fetchone()
    conn.close()
    return _order_row(row)


def count_open_orders(user_id: int) -> int:
    """Сколько у пользователя неоплаченных заказов — ограничитель на кнопку покупки."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM orders WHERE user_id = ? AND status = 'pending'",
        (user_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_orders_to_poll(max_age_days: int):
    """Заказы, по которым ещё имеет смысл спрашивать статус.

    Берём и pending, и expired: истёкший локально заказ мог быть оплачен в
    последний момент, и контрольные опросы после TTL — единственное, что ловит
    такой платёж. Какие из них опрашивать прямо сейчас, решает уже вызывающий
    по created_at/checked_at — строк тут единицы, фильтровать в SQL по времени
    незачем.
    """
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f'''
        SELECT {', '.join(_ORDER_FIELDS)} FROM orders
        WHERE status IN ('pending', 'expired') AND created_at >= ?
        ORDER BY created_at ASC
    ''', (cutoff,))
    rows = cursor.fetchall()
    conn.close()
    return [_order_row(row) for row in rows]


def mark_order_checked(invoice_id: int, state_code=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE orders SET checked_at = ?, state_code = COALESCE(?, state_code)
        WHERE invoice_id = ?
    ''', (datetime.now().isoformat(), state_code, invoice_id))
    conn.commit()
    conn.close()


def set_order_status(invoice_id: int, status: str, state_code=None) -> bool:
    """Переводит заказ в отменённый/протухший. Оплаченный не трогает никогда."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE orders
        SET status = ?, checked_at = ?, state_code = COALESCE(?, state_code)
        WHERE invoice_id = ? AND status <> 'paid'
    ''', (status, datetime.now().isoformat(), state_code, invoice_id))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def expire_stale_orders(ttl_hours: int) -> int:
    """Помечает 'expired' всё, что провисело в pending дольше TTL."""
    cutoff = (datetime.now() - timedelta(hours=ttl_hours)).isoformat()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE orders SET status = 'expired'
        WHERE status = 'pending' AND created_at < ?
    ''', (cutoff,))
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def _expiry_for(now: datetime, days: int | None) -> str:
    """Дата сгорания партии. days=None — партия вечная (реферальный бонус)."""
    return NEVER_EXPIRES if days is None else (now + timedelta(days=days)).isoformat()


def _insert_purchased(cursor, user_id: int, qty: int, expires_at: str,
                      source: str, invoice_id: int | None, now_iso: str):
    """Кладёт партию запросов курсором вызывающего — то есть в ЕГО транзакции."""
    cursor.execute('''
        INSERT INTO purchased_requests
            (user_id, qty, used, expires_at, source, invoice_id, created_at)
        VALUES (?, ?, 0, ?, ?, ?, ?)
    ''', (user_id, qty, expires_at, source, invoice_id, now_iso))


def _insert_purchased_once(cursor, user_id: int, qty: int, expires_at: str,
                           source: str, invoice_id: int | None, now_iso: str) -> bool:
    """Вставляет партию, только если по этому source у пользователя её ещё нет.

    Проверка и вставка — ОДИН оператор, и это обязательно: по одному и тому же
    source в базу одновременно ходят два пути (массовая раздача /giveall_subscribers
    и кнопка «Я подписался»). Раздельные SELECT и INSERT на разных соединениях
    оба увидели бы пусто и начислили дважды — ровно тот баг, что был у
    decrease_limit. Роль ключа идемпотентности играет сам source, как статус
    заказа в credit_order.

    Возвращает True, если партия действительно вставлена.
    """
    cursor.execute('''
        INSERT INTO purchased_requests
            (user_id, qty, used, expires_at, source, invoice_id, created_at)
        SELECT ?, ?, 0, ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM purchased_requests WHERE user_id = ? AND source = ?
        )
    ''', (user_id, qty, expires_at, source, invoice_id, now_iso, user_id, source))
    return cursor.rowcount > 0


def grant_once(user_id: int, qty: int, source: str,
               days: int | None = REQUEST_TTL_DAYS) -> str | None:
    """Разовое начисление партии по метке source. Дата сгорания или None.

    None означает «этому пользователю по этой метке уже начисляли» — не ошибка,
    а нормальный ответ, на который вызывающий отвечает «бонус у тебя уже есть».
    Для акций: одна метка на акцию, повторно она не сработает никогда.
    """
    now = datetime.now()
    expires_at = _expiry_for(now, days)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        inserted = _insert_purchased_once(cursor, user_id, qty, expires_at,
                                          source, None, now.isoformat())
        conn.commit()
    finally:
        conn.close()
    return expires_at if inserted else None


def has_grant(user_id: int, source: str) -> bool:
    """Начисляли ли уже партию с такой меткой. ТОЛЬКО для отчётов и сухого прогона.

    Как предварительная проверка перед начислением не годится: между этим
    SELECT и вставкой успевает вклиниться второй путь начисления. Решение
    «начислять или нет» принимает grant_once() одним оператором, и никак иначе.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT 1 FROM purchased_requests WHERE user_id = ? AND source = ? LIMIT 1',
        (user_id, source))
    found = cursor.fetchone() is not None
    conn.close()
    return found


def _accrue_referral(cursor, buyer_id: int, qty: int, invoice_id: int, now: datetime):
    """Начисляет бонусы пригласившему за покупку buyer_id. dict или None.

    qty — количество запросов, по которому считается бонус. Для пакета это то,
    что куплено; для подписки штук не существует, и вызывающий передаёт денежный
    эквивалент (см. credit_order, параметр ref_qty). Правила от вида покупки не
    зависят вовсе — потому здесь про подписку и не знают.

    Работает на курсоре вызывающего, то есть внутри транзакции credit_order, и
    это обязательно. Ключ идемпотентности здесь — статус заказа, а к оплаченному
    заказу опрос уже никогда не возвращается: начисли мы рефоводу отдельным
    шагом после коммита, падение между ними оставило бы заказ 'paid' с навсегда
    потерянным бонусом, о котором никто бы не узнал. Сеть (уведомления) остаётся
    снаружи — упавший send_message начисление не откатывает.

    Вызывать ПОСЛЕ того, как заказ уже помечен 'paid': признак первой покупки
    считается по COUNT(*) = 1, включая текущий заказ.
    """
    cursor.execute('SELECT referred_by FROM users WHERE user_id = ?', (buyer_id,))
    row = cursor.fetchone()
    if not row or not row[0]:
        return None

    referrer_id = row[0]
    if referrer_id == buyer_id:
        return None  # второй рубеж: самореферал отсекается ещё в set_referrer

    # Рефовода мог удалить самолечащийся /broadcast — начислять некому
    cursor.execute('SELECT 1 FROM users WHERE user_id = ?', (referrer_id,))
    if cursor.fetchone() is None:
        logging.warning("Реферал %s ссылается на удалённого пользователя %s — бонус пропущен",
                        buyer_id, referrer_id)
        return None

    # Первая ли это оплаченная покупка приглашённого. Текущий заказ уже помечен
    # 'paid' в этой же транзакции, поэтому "первая" — это ровно одна.
    # Гонок нет: в WAL писатели сериализуются, а транзакция держит write-lock
    # от первого UPDATE до commit, так что два одновременных зачисления
    # (поллер и кнопка "Проверить оплату") не могут оба увидеть единицу.
    cursor.execute(
        "SELECT COUNT(*) FROM orders WHERE user_id = ? AND status = 'paid'",
        (buyer_id,))
    first_purchase = cursor.fetchone()[0] == 1

    bonus = REF_FIRST_BONUS if (first_purchase and qty >= REF_BONUS_MIN_QTY) else 0
    percent = qty * REF_PERCENT // 100  # целочисленно: 5-9 → 1, 10-14 → 2, ...

    cursor.execute('''
        SELECT COALESCE(SUM(qty), 0) FROM purchased_requests
        WHERE user_id = ? AND source LIKE 'referral%'
    ''', (referrer_id,))
    earned_before = cursor.fetchone()[0]

    room = max(REF_MAX_TOTAL - earned_before, 0)
    capped = (bonus + percent) > room
    if capped:
        bonus = min(bonus, room)
        percent = min(percent, room - bonus)

    now_iso = now.isoformat()
    if bonus:
        _insert_purchased(cursor, referrer_id, bonus, NEVER_EXPIRES,
                          'referral_bonus', invoice_id, now_iso)
    if percent:
        _insert_purchased(cursor, referrer_id, percent, NEVER_EXPIRES,
                          'referral_percent', invoice_id, now_iso)

    return {
        'referrer_id': referrer_id,
        'buyer_id': buyer_id,
        'bonus': bonus,
        'percent': percent,
        'total': bonus + percent,
        'first_purchase': first_purchase,
        'capped': capped,
    }


def credit_order(invoice_id: int, state_code: int, op_key: str | None,
                 days: int = REQUEST_TTL_DAYS, ref_qty: int | None = None):
    """Отмечает заказ оплаченным и начисляет купленное. Возвращает dict или None.

    Что именно начисляется, решает orders.kind: 'requests' — партия запросов в
    purchased_requests (как было всегда), 'unlimited' — продление подписки в
    users.unlimited_until. Всё остальное — статусы, идемпотентность, реферальный
    бонус — у обоих видов общее, поэтому ветка ровно одна и внутри транзакции.

    ref_qty — сколько «запросов» стоит эта покупка для реферальной программы.
    Для пакета это его же qty, и параметр не нужен. Для подписки штук нет вовсе,
    поэтому эквивалент считает вызывающий (там, где живёт ценовая сетка) и
    передаёт сюда; без него подписка рефовода не кормит.

    None означает "начислять не надо": заказа нет или он уже оплачен — так
    работает защита от повторной обработки при следующем опросе, роль ключа
    идемпотентности играет сам статус заказа (у Stars ту же роль играл charge_id).

    Все записи — в одной транзакции, и это отличие от платежей Stars, где
    выдача и журнал шли по очереди. Там между ними был await и разные таблицы;
    здесь падение между UPDATE и INSERT означало бы заказ, помеченный оплаченным
    без начисления, к которому опрос уже никогда не вернётся. По той же причине
    внутрь транзакции попадает и реферальный бонус — см. _accrue_referral.
    """
    now = datetime.now()
    expires_at = _expiry_for(now, days)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            UPDATE orders
            SET status = 'paid', paid_at = ?, checked_at = ?, state_code = ?,
                op_key = COALESCE(?, op_key)
            WHERE invoice_id = ? AND status <> 'paid'
        ''', (now.isoformat(), now.isoformat(), state_code, op_key, invoice_id))

        if cursor.rowcount == 0:
            conn.rollback()
            return None

        cursor.execute(
            'SELECT user_id, qty, amount_rub, kind FROM orders WHERE invoice_id = ?',
            (invoice_id,))
        user_id, qty, amount_rub, kind = cursor.fetchone()

        if kind == ORDER_KIND_UNLIMITED:
            # qty у такого заказа — дни подписки. Партию запросов НЕ трогаем:
            # пакеты и подписка живут параллельно, и оплата одной не должна
            # ничего менять в другой.
            unlimited_until = _extend_unlimited(cursor, user_id, qty, now)
            expires_at = None
            if ref_qty is None:
                ref_qty = 0
        else:
            unlimited_until = None
            _insert_purchased(cursor, user_id, qty, expires_at,
                              'robokassa', invoice_id, now.isoformat())
            if ref_qty is None:
                ref_qty = qty

        referral = _accrue_referral(cursor, user_id, ref_qty, invoice_id, now)

        conn.commit()
        return {
            'invoice_id': invoice_id,
            'user_id': user_id,
            'qty': qty,
            'amount_rub': amount_rub,
            'kind': kind,
            'expires_at': expires_at,
            'unlimited_until': unlimited_until,
            'referral': referral,
        }
    finally:
        conn.close()


def add_purchased_requests(user_id: int, qty: int, days: int | None = REQUEST_TTL_DAYS,
                           source: str = 'admin', invoice_id: int | None = None) -> str:
    """Начисляет партию запросов вручную (/give). Возвращает дату сгорания.

    days=None — партия не сгорает никогда.
    """
    now = datetime.now()
    expires_at = _expiry_for(now, days)

    conn = get_connection()
    cursor = conn.cursor()
    _insert_purchased(cursor, user_id, qty, expires_at, source, invoice_id, now.isoformat())
    conn.commit()
    conn.close()
    return expires_at


def purchased_balance(user_id: int) -> int:
    """Сколько купленных запросов не потрачено и не сгорело."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COALESCE(SUM(qty - used), 0) FROM purchased_requests
        WHERE user_id = ? AND used < qty AND expires_at > ?
    ''', (user_id, datetime.now().isoformat()))
    balance = cursor.fetchone()[0]
    conn.close()
    return balance


def purchased_summary(user_id: int):
    """(остаток, когда сгорит ближайшая непотраченная партия или None).

    Остаток считается по всем живым партиям, включая вечные, а вот дата берётся
    только со сгораемых: иначе у человека с одними реферальными запросами в
    профиле стояло бы "сгорают 31.12.9999". Обе точки вызова уже умеют nearest
    = None (так же выглядит пустой баланс), поэтому UI менять не пришлось.
    """
    now_iso = datetime.now().isoformat()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COALESCE(SUM(qty - used), 0),
               MIN(CASE WHEN expires_at < ? THEN expires_at END)
        FROM purchased_requests
        WHERE user_id = ? AND used < qty AND expires_at > ?
    ''', (NEVER_EXPIRES, user_id, now_iso))
    balance, nearest = cursor.fetchone()
    conn.close()
    return balance, nearest


def eternal_balance(user_id: int) -> int:
    """Сколько из непотраченных запросов бессрочные (реферальные).

    Отдельной функцией, а не третьим полем purchased_summary: арность там менять
    ради строки, нужной только профилю и экрану /ref, дороже лишнего запроса.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COALESCE(SUM(qty - used), 0) FROM purchased_requests
        WHERE user_id = ? AND used < qty AND expires_at = ?
    ''', (user_id, NEVER_EXPIRES))
    balance = cursor.fetchone()[0]
    conn.close()
    return balance


# ==========================================
# 🎁 РЕФЕРАЛЬНАЯ ПРОГРАММА
# ==========================================

def set_referrer(user_id: int, referrer_id: int) -> bool:
    """Привязывает пригласившего. True — привязали, False — отказ.

    Один UPDATE, а не проверки в коде: условия стоят в WHERE, поэтому решение
    атомарно и не может разъехаться с параллельным /start. Отказ означает одно
    из: самореферал, такого рефовода нет в users, привязка уже стоит (задним
    числом её не переписать — иначе чужого приглашённого можно было бы отобрать).
    """
    if user_id == referrer_id:
        return False

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users SET referred_by = ?
        WHERE user_id = ?
          AND referred_by IS NULL
          AND ? <> user_id
          AND EXISTS (SELECT 1 FROM users WHERE user_id = ?)
    ''', (referrer_id, user_id, referrer_id, referrer_id))
    linked = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return linked


def get_referrer(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT referred_by FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def referral_stats(user_id: int):
    """(приглашено, из них купили хотя бы раз, заработано запросов всего)."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) FROM users WHERE referred_by = ?', (user_id,))
    invited = cursor.fetchone()[0]

    cursor.execute('''
        SELECT COUNT(DISTINCT o.user_id)
        FROM orders o
        JOIN users u ON u.user_id = o.user_id
        WHERE u.referred_by = ? AND o.status = 'paid'
    ''', (user_id,))
    paid = cursor.fetchone()[0]

    # По всем партиям, а не только по живым: это "сколько заработал за всё
    # время", и потраченные запросы из него пропадать не должны.
    cursor.execute('''
        SELECT COALESCE(SUM(qty), 0) FROM purchased_requests
        WHERE user_id = ? AND source LIKE 'referral%'
    ''', (user_id,))
    earned = cursor.fetchone()[0]

    conn.close()
    return invited, paid, earned


def top_referrers(limit: int = 10):
    """[(referrer_id, приглашено, заработано)] — для админского /refstats.

    Человеческий глаз при нынешних объёмах ловит накрутку надёжнее любой
    эвристики, поэтому смотреть на список — часть защиты, а не отчётность.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.referred_by,
               COUNT(*) AS invited,
               (SELECT COALESCE(SUM(p.qty), 0) FROM purchased_requests p
                 WHERE p.user_id = u.referred_by AND p.source LIKE 'referral%') AS earned
        FROM users u
        WHERE u.referred_by IS NOT NULL
        GROUP BY u.referred_by
        ORDER BY earned DESC, invited DESC
        LIMIT ?
    ''', (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


# ==========================================
# 📈 ИСТОЧНИКИ ТРАФИКА
# ==========================================
# Отдельным разделом, а не рядом с реферальной программой: acquisition_source и
# referred_by независимы (см. комментарий у DEFAULT_ACQUISITION_SOURCE), и
# соседство в файле намекало бы на связь, которой нет.

def set_acquisition_source(user_id: int, source: str) -> bool:
    """Проставляет метку источника трафика. True — проставили, False — отказ.

    Один UPDATE со всеми условиями в WHERE, как в set_referrer: решение атомарно
    и не разъезжается с параллельным /start. Отказ означает, что метка уже стоит
    ('organic' = ещё не присвоена, поэтому только из него и можно уйти) либо что
    писать нечего. Задним числом источник не переписывается — иначе второй
    диплинк переприписал бы пользователя другому каналу и цифры поехали бы.

    Валидацию тега делает вызывающий (bot.parse_src_payload): невалидный тег там
    просто не распознаётся, и источник остаётся 'organic'.
    """
    if not source or source == DEFAULT_ACQUISITION_SOURCE:
        return False

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users SET acquisition_source = ?
        WHERE user_id = ? AND acquisition_source = ?
    ''', (source, user_id, DEFAULT_ACQUISITION_SOURCE))
    written = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return written


def get_acquisition_source(user_id: int) -> str | None:
    """Метка источника или None, если такого пользователя нет."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT acquisition_source FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def acquisition_stats():
    """[(источник, пользователей, из них купивших, выручка в руб.)] — для /stats.

    Одним проходом: подзапрос сводит оплаченные заказы по пользователю, LEFT JOIN
    оставляет в выборке источники, где не купил никто — их и надо видеть, это
    неэффективные каналы. Сортировка по выручке, дальше по числу пользователей.

    Учёт за всё время. Оговорка: выручка привязывается к источнику через живую
    строку в users, а delete_user() (самолечение /broadcast по
    TelegramForbiddenError) строки удаляет, оставляя orders на месте — платежи
    удалённых пользователей в разрез по источникам не попадают. Сумма по строкам
    поэтому может быть меньше SUM(amount_rub) по всей таблице orders.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.acquisition_source,
               COUNT(*),
               COALESCE(SUM(CASE WHEN o.paid_orders > 0 THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(o.revenue), 0)
        FROM users u
        LEFT JOIN (
            SELECT user_id, COUNT(*) AS paid_orders, SUM(amount_rub) AS revenue
            FROM orders
            WHERE status = 'paid'
            GROUP BY user_id
        ) o ON o.user_id = u.user_id
        GROUP BY u.acquisition_source
        ORDER BY 4 DESC, 2 DESC, 1 ASC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return rows


def orphan_paid_revenue():
    """(выручка в руб., число платежей) по заказам без строки в users.

    Это ровно то, что выпадает из разреза acquisition_stats(): delete_user()
    (самолечение /broadcast по TelegramForbiddenError) удаляет пользователя, а
    orders оставляет, и такой платёж больше не привязан ни к какому источнику.
    Показываем его в /stats отдельной строкой, чтобы расхождение с общей выручкой
    было видно, а не пропадало молча.

    NOT IN безопасен: orders.user_id NOT NULL и users.user_id — PRIMARY KEY,
    NULL в подзапрос попасть не может (иначе NOT IN вернул бы пустоту).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COALESCE(SUM(amount_rub), 0), COUNT(*)
        FROM orders
        WHERE status = 'paid'
          AND user_id NOT IN (SELECT user_id FROM users)
    ''')
    revenue, payments = cursor.fetchone()
    conn.close()
    return revenue, payments

