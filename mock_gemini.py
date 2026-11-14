"""Заглушка Gemini для локальных прогонов (MOCK_GEMINI, см. config.py).

Подменяется САМЫЙ НИЖНИЙ уровень — `client.aio.models.generate_content`, а не
`collect_prices()` или `ask_gemini_two_step()`. Это сделано нарочно: так весь
код между API и пользователем остаётся под тестом — разбор строк PRICE_LINE_RE,
отсев по grounding_chunks, drop_outliers(), build_price_block(), extract_grounding(),
md_to_html(), truncate_html(). Заглушка на уровне ask_gemini_two_step() пропускала
бы мимо ровно ту логику, ради которой бота и гоняют локально.

Ответы собираются НАСТОЯЩИМИ классами SDK (types.GenerateContentResponse и
компания), поэтому структурная совместимость не «на глаз»: у объекта есть и
.text, и .candidates[0].finish_reason, и .grounding_metadata.grounding_chunks[].web.

Какой это шаг — определяется по наличию tools в config, а не по тексту промпта:
сборщик цен вызывается с GEMINI_TOOLS, вердикт — без них. Разъехаться такому
признаку не с чем.

А вот РЕЖИМ вердикта ('reseller' / 'buyer' / 'seller') по tools не отличить —
все идут без них. Заглушка узнаёт его по метке в system_instruction: словарь
{режим: метка} живёт в bot.py (VERDICT_PROMPT_MARKERS) и передаётся сюда
конструктором, копии текста здесь нет. Без этого локально нельзя было бы
глазами проверить /mode: все режимы отвечали бы одним и тем же текстом.

Метки есть не у всех режимов: у дефолтного ('reseller') её нет, и заглушка
отвечает им на всё, чего не узнала, — ровно как verdict_prompt() в bot.py
падает на DEFAULT_MODE.

Модуль удаляется целиком, если заглушка перестанет быть нужна: bot.py трогает
его в одном месте — там, где создаётся gemini_client.
"""

import asyncio
import logging

from google.genai import types

# Задержка на вызов: без неё локальный бот отвечает мгновенно, и глазами не
# видно ни «⏳ Анализирую», ни того, что слот gemini_semaphore реально занят.
MOCK_LATENCY_SEC = 1.0

# Ссылки, которые «нашёл поиск». Домены отсюда попадают в grounding_chunks, и
# collect_prices() отбрасывает цены с доменами, которых в чанках нет, — поэтому
# домены в строках цен ниже обязаны совпадать с этими.
_MOCK_CHUNKS = [
    ("https://www.avito.ru/moskva/telefony/iphone_13_128gb_1", "avito.ru"),
    ("https://www.avito.ru/moskva/telefony/iphone_13_128gb_2", "avito.ru"),
    ("https://youla.ru/moskva/elektronika/iphone-13-128", "youla.ru"),
    ("https://www.avito.ru/moskva/telefony/iphone_13_128gb_3", "avito.ru"),
    ("https://market.yandex.ru/product--smartfon-apple-iphone-13", "market.yandex.ru"),
]

# Ответ шага 1 в формате «ЦЕНА | ДОМЕН | РЕГИОН | описание». Намеренно смешаны
# частные объявления и магазин (market.yandex.ru): розницу код обязан выкинуть.
_MOCK_PRICE_LINES = """18500 | avito.ru | Москва | iPhone 13 128GB б/у, синий, аккум 89%
19900 | avito.ru | Москва | iPhone 13 128GB, полный комплект, чек
19200 | youla.ru | Москва | iPhone 13 128 ГБ, состояние отличное
21000 | avito.ru | Московская область | iPhone 13 128GB, коробка, чехол
36990 | market.yandex.ru | Москва | Apple iPhone 13 128GB, новый"""

# Вердикт шага 2. Формат — строго тот же шаблон, что требует
# SYSTEM_PROMPT_GIVEN_PRICES, вместе с GFM-жирным (**), который md_to_html()
# обязан превратить в <b>. Числа согласованы с ценами выше.
_MOCK_VERDICT = """📱 **Товар:** Apple iPhone 13 128GB, б/у, состояние хорошее
💰 **Цена продавца:** 17 000 руб.
📊 **Рынок:** 18 500 - 21 000 руб. (Москва и Московская область)
🔎 **Источник цены:** По найденным объявлениям
🟢 **Запас маржи:** 2 000 - 3 500 руб. чистыми после торга и подготовки
⚠️ **Подводные камни:** По фото не видно состояния корпуса с торцов, нет скриншота
"Об этом устройстве". Аккумулятор ниже 85% — минус к цене. Комплект неполный.
⚖️ **Вердикт:** Торговаться до 15 500. Ликвид хороший, уходит за 3-5 дней, но
без проверки Face ID и оригинальности дисплея в руках не берём.

_Оценка по фото предварительная, живой осмотр обязателен._

[ЗАГЛУШКА MOCK_GEMINI — реальный запрос к Gemini не отправлялся]"""

# Тот же вердикт для ветки «частных цен не нашлось»: вилка шире, а в строке
# источника — дисклеймер. Сам текст дисклеймера сюда передаёт bot.py
# (NO_DATA_DISCLAIMER остаётся единственным местом, где он записан).
_MOCK_VERDICT_NO_DATA = """📱 **Товар:** Apple iPhone 13 128GB, б/у, состояние хорошее
💰 **Цена продавца:** 17 000 руб.
📊 **Рынок:** 17 000 - 24 000 руб. (Москва и Московская область)
🔎 **Источник цены:** {disclaimer}
🟢 **Запас маржи:** Считать рано — вилка широкая, рынок не проверен
⚠️ **Подводные камни:** Свежих частных объявлений найти не удалось, цифры даны
по общим знаниям о рынке и могут быть неточными. Комплект неполный.
⚖️ **Вердикт:** Рискованно. Без проверки рынка руками в такую сделку не заходим.

[ЗАГЛУШКА MOCK_GEMINI — реальный запрос к Gemini не отправлялся]"""


# Вердикт режима «покупаю для себя»: другой набор строк формата, никакой
# маржи. Нужен ровно для того, чтобы после /mode было видно, что промпт
# действительно сменился.
_MOCK_VERDICT_BUYER = """📱 **Товар:** Apple iPhone 13 128GB, б/у, состояние хорошее
💰 **Цена продавца:** 17 000 руб.
📊 **Рынок:** 18 500 - 21 000 руб. (Москва и Московская область)
🔎 **Источник цены:** По найденным объявлениям
💸 **Справедливость цены:** Дешевле рынка примерно на 1 500 - 3 000 руб. Просить
скидку особо не с чего, цена и так ниже средней.
🔍 **Что проверить при встрече:** Ёмкость аккумулятора в «Об этом устройстве»,
работу Face ID, оба динамика, все камеры, отсутствие следов вскрытия по винтам,
привязку к чужому Apple ID (сделать сброс при тебе).
⚠️ **Риски и красные флаги:** Комплект неполный, торцы корпуса на фото не видно,
аккумулятор ниже 85% — менять в ближайший год.
⚖️ **Стоит ли брать:** Брать только после проверки. Для личного пользования
экземпляр нормальный, переплаты нет; если Face ID не работает — проходим мимо.

_Оценка по фото предварительная, живой осмотр обязателен._

[ЗАГЛУШКА MOCK_GEMINI — реальный запрос к Gemini не отправлялся]"""


# Вердикт режима «продаю свою вещь»: снова другой набор строк формата, никакой
# маржи и никакого «стоит ли брать» — цена размещения и подготовка объявления.
_MOCK_VERDICT_SELLER = """📱 **Товар:** Apple iPhone 13 128GB, б/у, состояние хорошее
💰 **Ваша цена:** 17 000 руб.
📊 **Рынок:** 18 500 - 21 000 руб. (Москва и Московская область)
🔎 **Источник цены:** По найденным объявлениям
🏷 **Цена размещения:** Для быстрой продажи: 19 000 руб. / Если не спешите:
21 500 руб. Обе с запасом на торг примерно 1 500 руб., ниже 18 000 не опускайтесь.
📸 **Как подать в объявлении:** Скриншот «Об этом устройстве» с ёмкостью
аккумулятора, фото всех четырёх торцов и углов, снимок включённого экрана на
белом фоне, коробка с совпадающим IMEI, чек или дата покупки в описании.
⚠️ **На что будут давить при торге:** Ёмкость аккумулятора ниже 85%, неполный
комплект, отсутствие чека. Первые два обоснованы, третье — обычная попытка
сбить цену: дата активации видна в настройках, покажите её на встрече.
⚖️ **Итог:** Ставить 21 500 и подождать неделю — позиция ликвидная, спешить
незачем. Ваши 17 000 — заметно ниже рынка.

_Оценка по фото предварительная, живой осмотр обязателен._

[ЗАГЛУШКА MOCK_GEMINI — реальный запрос к Gemini не отправлялся]"""


# Реестр вердиктов по режимам. Ключи — те же, что у VERDICT_PROMPT_MARKERS в
# bot.py; режим без своей строки получит _MOCK_VERDICT.
_MOCK_VERDICTS = {
    "buyer": _MOCK_VERDICT_BUYER,
    "seller": _MOCK_VERDICT_SELLER,
}


def _response(text: str, with_grounding: bool):
    """Собирает объект ответа настоящими типами SDK."""
    meta = None
    if with_grounding:
        meta = types.GroundingMetadata(
            web_search_queries=["iphone 13 128gb москва авито б/у"],
            grounding_chunks=[
                # domain у живых чанков приходит пустым, домен лежит в title —
                # воспроизводим это, иначе заглушка не проверяла бы chunk_domains()
                types.GroundingChunk(web=types.GroundingChunkWeb(uri=uri, title=title))
                for uri, title in _MOCK_CHUNKS
            ],
        )
    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            finish_reason=types.FinishReason.STOP,
            grounding_metadata=meta,
        )]
    )


class _MockModels:
    def __init__(self, mode: str, no_data_disclaimer: str,
                 verdict_markers: dict | None = None):
        self._mode = mode
        self._disclaimer = no_data_disclaimer
        self._markers = verdict_markers or {}

    async def generate_content(self, *, model, contents, config=None, **kwargs):
        await asyncio.sleep(MOCK_LATENCY_SEC)

        # Шаг 1 (сбор цен) идёт с tools, шаг 2 (вердикт) — без них
        is_collector = bool(getattr(config, "tools", None))

        if is_collector:
            if self._mode == "fail":
                raise RuntimeError("MOCK_GEMINI=fail: имитация сбоя сбора цен")
            if self._mode == "nodata":
                logging.info("MOCK_GEMINI: сборщик цен вернул пустой список")
                return _response("НЕТ ДАННЫХ", with_grounding=True)
            logging.info("MOCK_GEMINI: отдаю тестовый список цен")
            return _response(_MOCK_PRICE_LINES, with_grounding=True)

        # Вердикт подстраиваем под то, что реально доехало до шага 2: блок цен
        # приезжает последним элементом contents. Так заглушка проходит ту же
        # развилку, что и живая модель, и ветку «НЕТ ДАННЫХ» можно посмотреть
        # глазами (MOCK_GEMINI=nodata).
        tail = str(contents[-1]) if contents else ""
        if "НЕТ ДАННЫХ" in tail:
            logging.info("MOCK_GEMINI: вердикт по ветке «цен не нашлось»")
            return _response(
                _MOCK_VERDICT_NO_DATA.format(disclaimer=self._disclaimer),
                with_grounding=False,
            )

        # Режим ищем перебором меток, а не двумя if: режимов три, и любой
        # новый должен попадать сюда добавлением строки в _MOCK_VERDICTS, а не
        # правкой условия.
        instruction = str(getattr(config, "system_instruction", "") or "")
        for verdict_mode, marker in self._markers.items():
            if marker and marker in instruction:
                logging.info("MOCK_GEMINI: вердикт для режима %s", verdict_mode)
                return _response(_MOCK_VERDICTS.get(verdict_mode, _MOCK_VERDICT),
                                 with_grounding=False)

        logging.info("MOCK_GEMINI: обычный вердикт (режим по умолчанию)")
        return _response(_MOCK_VERDICT, with_grounding=False)


class _MockAio:
    def __init__(self, models):
        self.models = models


class MockGeminiClient:
    """Утиный двойник genai.Client: боту нужен только .aio.models.generate_content."""

    def __init__(self, mode: str, no_data_disclaimer: str,
                 verdict_markers: dict | None = None):
        models = _MockModels(mode, no_data_disclaimer, verdict_markers)
        self.aio = _MockAio(models)
        self.models = models  # синхронной ветки в боте нет, но пусть будет симметрично
