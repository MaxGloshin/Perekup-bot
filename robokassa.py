"""Robokassa: ссылка на оплату и опрос статуса заказа (OpStateExt).

Схема без webhook: ResultURL не используется, домена у бота нет. Заказ создаётся
в БД до оплаты, дальше фоновая задача опрашивает OpStateExt по InvId.

Что важно знать про этот API (проверено живыми платежами 04.08.2026, магазин
perekupru, два счёта по 1 руб., оба дошли до State.Code=100):

- Подпись ссылки: MD5("MerchantLogin:OutSum:InvId:Receipt:Пароль#1") — вариант
  документации, он же режим 'raw' и умолчание. Receipt входит в подпись как
  минифицированный JSON. Проверено полностью: оплата прошла И чек через
  Робочеки СМЗ сформировался и пришёл на почту покупателя.
  Вариант вовсе без Receipt (MD5("MerchantLogin:OutSum:InvId:Пароль#1"), режим
  'none') оплату тоже пропускает, но чека по нему нет — номенклатуру собирать
  не из чего. Для прода он непригоден, только для диагностики подписи.
  Подпись с Receipt заодно закрывает подмену: без неё номенклатуру в ссылке
  можно переписать руками, и в чек уедет что угодно.
- **Ошибка 29 ("неверная цифровая подпись") НЕ означает, что неправильно
  закодирован Receipt.** На этом потеряно несколько заходов: 29 приходила
  одинаково на 'raw', на 'encoded' и на ссылку вовсе без Receipt, а причиной
  был несохранённый Пароль#1 в личном кабинете. Прежде чем крутить кодирование,
  проверьте пароль минимальной ссылкой: check_robokassa.py link --no-receipt.
  Если не открывается и она — дело в пароле или в магазине, а не в чеке.
  Строка OutSum в подписи и в URL должна совпадать посимвольно — поэтому
  формат считается один раз (_format_sum) и используется в обоих местах.
- ExpirationDate в подпись не входит и НЕ отправляется: в официальном примере
  его нет, а неверный формат ломает счёт. Срок жизни неоплаченного заказа
  держим у себя (db.expire_stale_orders), ссылка при этом остаётся живой —
  ради поздних оплат в bot.py есть длинное окно контрольных опросов.
- Подпись опроса: MD5("MerchantLogin:InvoiceID:Пароль#2") — ДРУГОЙ пароль.
- OpStateExt работает только в боевом режиме: про платежи с IsTest=1 он ничего
  не знает и всегда отвечает Result.Code=3.
- Result.Code=3 ("операция не найдена") — штатный ответ для заказа, который ещё
  не оплачивали. Это не ошибка: Robokassa узнаёт об InvId в момент первого
  обращения к платёжной форме, а строку в orders мы создаём раньше.

В Robokassa уходит только номер заказа (InvId) — не Telegram ID пользователя.
Сопоставление заказа с пользователем живёт в нашей БД, и п. 5.3 /privacy описан
именно так.
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from xml.etree import ElementTree

import aiohttp  # транзитивная зависимость aiogram, отдельно ставить не нужно

# Переменные окружения разбирает config (он же выбирает .env или .env.local).
# Собственный load_dotenv() здесь был бы вреден: он не перетирает уже
# выставленные переменные, но ДОБАВЛЯЕТ из .env всё, чего нет в .env.local, —
# и локальный бот пошёл бы в боевой магазин Robokassa.
import config  # noqa: F401  — импорт ради побочного эффекта (загрузка окружения)

PAYMENT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
OPSTATE_URL = "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt"

MERCHANT_LOGIN = os.getenv("ROBOKASSA_LOGIN")
PASSWORD_1 = os.getenv("ROBOKASSA_PASS1")
PASSWORD_2 = os.getenv("ROBOKASSA_PASS2")
IS_TEST = os.getenv("ROBOKASSA_IS_TEST", "0").strip() == "1"

# Участвует ли Receipt в строке подписи:
#   "raw"     — минифицированный JSON внутри подписи. Единственный режим,
#               подтверждённый боевым платежом (оплата + реальный чек СМЗ);
#               значение по умолчанию, менять его для прода не на что.
#   "encoded" — он же после URL-кодирования. Не проверен.
#   "none"    — не участвует: MD5("MerchantLogin:OutSum:InvId:Пароль#1"),
#               как в официальном примере. Оплата проходит, чек не формируется —
#               только для диагностики подписи, не для боевой работы.
RECEIPT_SIGN_MODES = ("raw", "encoded", "none")
RECEIPT_SIGN_MODE = os.getenv("ROBOKASSA_RECEIPT_SIGN", "raw").strip().lower()

# Формат OutSum. "fixed2" — 1.00 (обычный вид суммы у Robokassa), "plain" — 1
# (как в официальном примере, где cost уходит без форматирования). Влияет
# и на подпись, и на URL сразу: строка обязана быть одна и та же.
SUM_FORMATS = ("fixed2", "plain")
SUM_FORMAT = os.getenv("ROBOKASSA_SUM_FORMAT", "fixed2").strip().lower()

_CREDS = (MERCHANT_LOGIN, PASSWORD_1, PASSWORD_2)

# Приём оплаты выключается отсутствием ключей целиком: дев-копия должна
# запускаться без реквизитов Robokassa. А вот половина ключей — это опечатка
# в .env на боевом сервере, и молча стартовать с ней нельзя.
PAYMENTS_ENABLED = all(_CREDS)
if any(_CREDS) and not PAYMENTS_ENABLED:
    raise RuntimeError(
        "В .env заданы не все реквизиты Robokassa. Нужны сразу три: "
        "ROBOKASSA_LOGIN, ROBOKASSA_PASS1, ROBOKASSA_PASS2 "
        "(либо ни одного — тогда оплата в боте выключена)."
    )

# Таймаут одного обращения к OpStateExt. Опрос живёт на общем event loop,
# висеть на сокете минутами ему нельзя.
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Робочеки СМЗ: самозанятый без НДС и без системы налогообложения в чеке,
# услуга оплачивается целиком в момент покупки.
RECEIPT_TAX = "none"
RECEIPT_PAYMENT_METHOD = "full_payment"
RECEIPT_PAYMENT_OBJECT = "service"
# Description в ссылке Robokassa ограничен 100 символами
MAX_DESCRIPTION_LEN = 100


class RobokassaConfigError(RuntimeError):
    """Реквизиты Robokassa не заданы, а их запросили."""


def _require_creds():
    if not PAYMENTS_ENABLED:
        raise RobokassaConfigError(
            "Оплата не настроена: в .env нет ROBOKASSA_LOGIN/PASS1/PASS2."
        )


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _format_sum(amount_rub: int, style: str | None = None) -> str:
    """OutSum одной строкой для подписи и для URL.

    Robokassa сверяет подпись по той строке, что пришла в запросе, поэтому
    форматируем ровно один раз и используем результат в обоих местах.
    Никаких "1.000000" тут быть не может: обе ветки дают либо "1.00", либо "1".
    """
    style = (style or SUM_FORMAT).lower()
    if style == "plain":
        return str(int(amount_rub))
    return f"{amount_rub:.2f}"


def build_receipt(qty: int, amount_rub: int, item_name: str | None = None) -> dict:
    """Чек для Робочеков СМЗ: одна номенклатурная позиция на всю покупку.

    Позиция одна, а не qty штук: сумма позиции обязана сойтись с OutSum, а
    дробить 120 запросов на 120 строк чека незачем — услуга оказывается пакетом.

    item_name задаёт наименование, когда покупаются не запросы (безлимитная
    подписка): qty там означает дни, и «Анализ объявления, 30 шт.» в чеке было
    бы прямой неправдой.
    """
    return {
        "items": [
            {
                "name": item_name or receipt_item_name(qty),
                "quantity": 1,
                "sum": float(amount_rub),
                "payment_method": RECEIPT_PAYMENT_METHOD,
                "payment_object": RECEIPT_PAYMENT_OBJECT,
                "tax": RECEIPT_TAX,
            }
        ]
    }


def receipt_item_name(qty: int) -> str:
    return f"Анализ объявления в боте, {qty} шт."


def _minified(receipt: dict) -> str:
    # separators без пробелов: JSON должен быть минифицированным,
    # ensure_ascii=False — кириллица уходит в UTF-8, как требует документация.
    return json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))


def build_payment_url(
    invoice_id: int,
    amount_rub: int,
    qty: int,
    description: str = "",
    item_name: str | None = None,
    expires_at: datetime | None = None,
    receipt_sign_mode: str | None = None,
    include_receipt: bool = True,
    sum_format: str | None = None,
    is_test: bool | None = None,
) -> str:
    """Ссылка на оплату для конкретного заказа.

    invoice_id должен быть уже выделен в orders: подпись считается по нему,
    и второй ссылки с тем же номером быть не должно.

    item_name перебивает наименование позиции в чеке и в Description. Нужен
    для товаров, которые считаются не в запросах (безлимитная подписка).

    include_receipt=False собирает минимальную ссылку из официального примера
    Robokassa — без номенклатуры вообще. Годится для диагностики подписи, но
    не для боевой работы: без Receipt чек Робочеков СМЗ не сформируется.
    """
    _require_creds()

    out_sum = _format_sum(amount_rub, sum_format)

    mode = (receipt_sign_mode or RECEIPT_SIGN_MODE).lower()
    if mode not in RECEIPT_SIGN_MODES:
        raise ValueError(f"Неизвестный режим подписи Receipt: {mode!r}")
    if not include_receipt:
        mode = "none"  # подписывать нечего, чека в запросе нет

    params = {
        "MerchantLogin": MERCHANT_LOGIN,
        "OutSum": out_sum,
        "InvId": invoice_id,
        "Description": (description or item_name
                        or receipt_item_name(qty))[:MAX_DESCRIPTION_LEN],
    }

    if include_receipt:
        receipt_json = _minified(build_receipt(qty, amount_rub, item_name))
        # quote, а не quote_plus: пробел кодируется как %20, а не как '+'. Плюс в
        # query-строке означает пробел только по соглашению форм, и если приёмник
        # его так не читает, в чек уедет "Анализ+объявления".
        receipt_encoded = quote(receipt_json, safe="")
        params["Receipt"] = receipt_json  # urlencode закодирует его ровно один раз
    else:
        receipt_json = receipt_encoded = ""

    if mode == "none":
        signature = _md5(f"{MERCHANT_LOGIN}:{out_sum}:{invoice_id}:{PASSWORD_1}")
    else:
        receipt_for_signature = receipt_json if mode == "raw" else receipt_encoded
        signature = _md5(
            f"{MERCHANT_LOGIN}:{out_sum}:{invoice_id}:{receipt_for_signature}:{PASSWORD_1}"
        )

    params["SignatureValue"] = signature
    params["Culture"] = "ru"
    params["Encoding"] = "utf-8"

    # ExpirationDate по умолчанию не отправляем: в подпись он не входит, но
    # неверный формат ломает счёт, а в официальном примере его нет.
    if expires_at is not None:
        params["ExpirationDate"] = format_expiration(expires_at)
    if IS_TEST if is_test is None else is_test:
        params["IsTest"] = 1

    return f"{PAYMENT_URL}?{urlencode(params, quote_via=quote)}"


def format_expiration(moment: datetime) -> str:
    """ExpirationDate в ISO 8601 со смещением зоны.

    Формат подписью не покрыт (ExpirationDate в неё не входит), поэтому ошибка
    здесь ломает не подпись, а сам счёт — параметр проверяется тем же тестовым
    платежом, что и Receipt.
    """
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.isoformat(timespec="milliseconds")


@dataclass
class OpState:
    """Разобранный ответ OpStateExt."""

    result_code: int
    result_description: str
    state_code: int | None
    out_sum: float | None
    inc_sum: float | None
    op_key: str | None
    raw: str

    # Деньги зачислены магазину — единственное состояние, по которому начисляем.
    PAID = 100
    # Деньги получены от покупателя, но магазину ещё не зачислены.
    RECEIVED = 50
    HOLD = 20
    SECURITY_CHECK = 80
    CANCELED = 10
    REFUNDED = 60

    # Result.Code
    OK = 0
    BAD_SIGNATURE = 1
    NO_SUCH_SHOP = 2
    NOT_FOUND = 3
    DUPLICATE_INVOICE = 4
    SERVICE_ERROR = 1000

    @property
    def is_paid(self) -> bool:
        return self.result_code == self.OK and self.state_code == self.PAID

    @property
    def is_canceled(self) -> bool:
        return self.result_code == self.OK and self.state_code in (
            self.CANCELED,
            self.REFUNDED,
        )

    @property
    def not_started(self) -> bool:
        """Robokassa про этот InvId ещё не слышала — заказ просто не оплачивали."""
        return self.result_code == self.NOT_FOUND

    @property
    def is_config_error(self) -> bool:
        """Битая подпись или чужой магазин: код чинить, опрос продолжать бессмысленно."""
        return self.result_code in (self.BAD_SIGNATURE, self.NO_SUCH_SHOP)


def _local_name(tag: str) -> str:
    # У ответа есть namespace (merchant.roboxchange.com). Сравнивать полное имя
    # хрупко — при его смене разбор молча перестанет находить поля.
    return tag.rsplit("}", 1)[-1]


def _find(root, *path):
    node = root
    for name in path:
        node = next(
            (child for child in node if _local_name(child.tag) == name),
            None,
        )
        if node is None:
            return None
    return node


def _text(node):
    return node.text.strip() if node is not None and node.text else None


def _to_float(value):
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def parse_op_state(xml_text: str) -> OpState:
    """Разбор XML OpStateExt. Отдельной функцией — тестируется без сети.

    Всё, что здесь выглядит перестраховкой, взято из реального ответа боевого
    магазина (проверено платежом 04.08.2026):

    - ответ приходит с BOM в начале — снимаем его перед разбором;
    - namespace на самом деле `http://merchant.roboxchange.com/WebService/`,
      а не тот, что в примерах документации, — поэтому теги сравниваются по
      локальному имени, иначе разбор молча вернул бы пустоту;
    - у `Result` бывает только `Code`, без `Description` — отсюда пустая строка
      по умолчанию;
    - суммы приходят в виде `1.000000`, поэтому сверка с ценой заказа идёт
      с допуском, а не по равенству строк;
    - `Info` содержит и то, что нам не нужно (маскированный номер карты
      `IncAccount`, `BankCardRRN`). Ничего из этого мы не читаем и не храним —
      в orders уходит только OpKey.
    """
    root = ElementTree.fromstring(xml_text.lstrip("﻿").strip())

    result = _find(root, "Result")
    code_text = _text(_find(result, "Code")) if result is not None else None
    if code_text is None:
        raise ValueError(f"В ответе OpStateExt нет Result/Code: {xml_text[:300]}")

    state = _find(root, "State")
    info = _find(root, "Info")

    state_code_text = _text(_find(state, "Code")) if state is not None else None

    return OpState(
        result_code=int(code_text),
        result_description=_text(_find(result, "Description")) or "",
        state_code=int(state_code_text) if state_code_text else None,
        out_sum=_to_float(_text(_find(info, "OutSum"))) if info is not None else None,
        inc_sum=_to_float(_text(_find(info, "IncSum"))) if info is not None else None,
        op_key=_text(_find(info, "OpKey")) if info is not None else None,
        raw=xml_text,
    )


async def get_op_state(session: aiohttp.ClientSession, invoice_id: int) -> OpState:
    """Спрашивает статус одного заказа. Сетевые ошибки пробрасывает наверх."""
    _require_creds()

    params = {
        "MerchantLogin": MERCHANT_LOGIN,
        "InvoiceID": invoice_id,
        "Signature": _md5(f"{MERCHANT_LOGIN}:{invoice_id}:{PASSWORD_2}"),
    }
    async with session.get(OPSTATE_URL, params=params, timeout=HTTP_TIMEOUT) as resp:
        resp.raise_for_status()
        # Сервис отдаёт text/xml; читаем текстом, чтобы разбирать одним парсером
        body = await resp.text()

    state = parse_op_state(body)
    logging.debug(
        "OpStateExt InvId=%s -> Result.Code=%s State.Code=%s",
        invoice_id,
        state.result_code,
        state.state_code,
    )
    return state


def expiration_for(created_at: datetime, hours: int) -> datetime:
    return created_at.astimezone(timezone.utc) + timedelta(hours=hours)
