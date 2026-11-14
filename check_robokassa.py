"""Ручная проверка связки с Robokassa: сформировать ссылку и опросить статус.

Нужна потому, что OpStateExt не видит тестовые платежи (IsTest=1) — сквозной
путь «ссылка → оплата → чек СМЗ → опрос → статус 100» проверяется только
реальным платежом. Скрипт ничего не пишет в perekup.db и не трогает бота.

Порядок проверки:

    .venv\\Scripts\\python.exe check_robokassa.py link --amount 1
    (открыть ссылку, оплатить 1 руб. картой, дождаться чека)
    .venv\\Scripts\\python.exe check_robokassa.py state --invid <InvId из вывода>

Если платёжная форма ругается на подпись — тот же link с другим режимом:

    .venv\\Scripts\\python.exe check_robokassa.py link --amount 1 --receipt-sign encoded

Рабочий режим потом фиксируется в .env (ROBOKASSA_RECEIPT_SIGN) и в CLAUDE.md.
"""

import argparse
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import aiohttp

import robokassa


def make_invoice_id() -> int:
    """Номер для ручной проверки — unix-time в МИЛЛИСЕКУНДАХ.

    Секунд не хватает: два запуска подряд укладываются в одну секунду и получают
    один номер, а два счёта с одним InvId — это Result.Code=4 при опросе, то есть
    заказ, статус которого уже не определить.

    Заведомо не пересечётся с боевыми заказами: их InvId растут автоинкрементом
    от INVOICE_ID_BASE (порядок 10^6), а тут порядок 10^12. Верхняя граница
    InvId у Robokassa — 9223372036854775807, запас огромный.
    """
    return int(time.time() * 1000)


def cmd_link(args):
    invoice_id = args.invid or make_invoice_id()
    expires_at = None
    if args.expire_hours:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=args.expire_hours)

    include_receipt = not args.no_receipt
    mode = args.receipt_sign or robokassa.RECEIPT_SIGN_MODE
    if not include_receipt:
        mode = "none"
    out_sum = robokassa._format_sum(args.amount, args.sum_format)

    url = robokassa.build_payment_url(
        invoice_id=invoice_id,
        amount_rub=args.amount,
        qty=args.qty,
        expires_at=expires_at,
        receipt_sign_mode=args.receipt_sign,
        include_receipt=include_receipt,
        sum_format=args.sum_format,
        is_test=True if args.test else None,
    )

    # Строка, по которой считается MD5 — с паролем, замененным на заглушку.
    # Именно её сверяют с официальным примером, когда ловят ошибку 29.
    if mode == "none":
        template = f"{robokassa.MERCHANT_LOGIN}:{out_sum}:{invoice_id}:<Пароль#1>"
    else:
        template = (f"{robokassa.MERCHANT_LOGIN}:{out_sum}:{invoice_id}:"
                    f"<Receipt {mode}>:<Пароль#1>")

    print(f"InvId:            {invoice_id}")
    print(f"OutSum:           {out_sum}")
    print(f"Receipt в ссылке: {'да' if include_receipt else 'НЕТ'}")
    print(f"Receipt в подписи:{' нет' if mode == 'none' else ' ' + mode}")
    print(f"Строка подписи:   MD5(\"{template}\")")
    print(f"ExpirationDate:   {robokassa.format_expiration(expires_at) if expires_at else 'не отправляется'}")
    print(f"IsTest:           {'1' if (args.test or robokassa.IS_TEST) else '0'}")
    if include_receipt:
        print("Receipt:")
        print(json.dumps(robokassa.build_receipt(args.qty, args.amount),
                         ensure_ascii=False, indent=2))
    print("\nСсылка на оплату:\n")
    print(url)
    print(f"\nПосле оплаты:  python check_robokassa.py state --invid {invoice_id}")


async def _fetch_state(invoice_id: int):
    async with aiohttp.ClientSession() as session:
        return await robokassa.get_op_state(session, invoice_id)


def cmd_state(args):
    state = asyncio.run(_fetch_state(args.invid))

    print(f"Result.Code:      {state.result_code} ({state.result_description})")
    print(f"State.Code:       {state.state_code}")
    print(f"Info.OutSum:      {state.out_sum}")
    print(f"Info.IncSum:      {state.inc_sum}")
    print(f"Info.OpKey:       {state.op_key}")
    print(f"Оплачен:          {state.is_paid}")

    if state.not_started:
        print("\n→ Robokassa про этот InvId не знает. Для неоплаченного заказа это норма.")
    elif state.is_config_error:
        print("\n→ Проблема в реквизитах: неверная подпись (Пароль#2) или чужой магазин.")
    elif state.result_code == state.DUPLICATE_INVOICE:
        print("\n→ Две операции с одним InvId. Номер переиспользован — разбирать в ЛК.")

    if args.raw:
        print("\nСырой XML:\n")
        print(state.raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_link = sub.add_parser("link", help="сформировать ссылку на оплату")
    p_link.add_argument("--amount", type=int, default=1, help="сумма в рублях (по умолчанию 1)")
    p_link.add_argument("--qty", type=int, default=1, help="сколько запросов в чеке")
    p_link.add_argument("--invid", type=int, help="номер заказа (по умолчанию unix-time)")
    p_link.add_argument("--expire-hours", type=int, default=0,
                        help="ExpirationDate через N часов; 0 (по умолчанию) — не передавать")
    p_link.add_argument("--receipt-sign", choices=robokassa.RECEIPT_SIGN_MODES,
                        help="участие Receipt в подписи: none (по умолчанию), raw, encoded")
    p_link.add_argument("--no-receipt", action="store_true",
                        help="минимальная ссылка без Receipt вообще (как в примере Robokassa)")
    p_link.add_argument("--sum-format", choices=robokassa.SUM_FORMATS, default=None,
                        help="формат OutSum: fixed2 (1.00, по умолчанию) или plain (1)")
    p_link.add_argument("--test", action="store_true",
                        help="IsTest=1 — только с ТЕСТОВЫМИ паролями магазина")
    p_link.set_defaults(func=cmd_link)

    p_state = sub.add_parser("state", help="опросить статус заказа через OpStateExt")
    p_state.add_argument("--invid", type=int, required=True)
    p_state.add_argument("--raw", action="store_true", help="показать сырой XML ответа")
    p_state.set_defaults(func=cmd_state)

    args = parser.parse_args()
    try:
        args.func(args)
    except robokassa.RobokassaConfigError as e:
        # Обычный случай на дев-копии: ключей нет. Трейсбек тут ничего не добавляет.
        raise SystemExit(f"{e}\nЗаполни .env по образцу .env.example и запусти снова.")


if __name__ == "__main__":
    main()
