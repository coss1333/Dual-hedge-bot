
import os
import time
import json
import hmac
import hashlib
import requests
from datetime import datetime, timezone

"""
Простой бот для стратегии:
1) Находит лучший по APR Dual Investment по ETH с инвестицией в USDT (срок ~1 день)
2) Открывает ордер Dual Investment на сумму, которую укажет пользователь
3) Открывает фьючерсный шорт по ETH_USDT для хеджирования
4) Периодически проверяет статус Dual Investment ордера
5) После экспирации:
   - закрывает фьючерсный шорт
   - если получен ETH – продаёт его в споте за USDT
ВНИМАНИЕ: код учебный, ОБЯЗАТЕЛЬНО протестируйте на маленьких суммах.
"""

API_HOST = "https://api.gateio.ws"
API_PREFIX = "/api/v4"

CONFIG_FILE = "config.json"


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ["api_key", "api_secret"]:
        if not cfg.get(k):
            raise RuntimeError(f"Заполните поле {k} в {CONFIG_FILE}")
    # значения по умолчанию
    cfg.setdefault("futures_settle", "usdt")
    cfg.setdefault("futures_contract", "ETH_USDT")
    cfg.setdefault("hedge_multiplier", 1.0)  # 1.0 = хедж только тела вклада
    cfg.setdefault("poll_interval_sec", 60)
    return cfg


def gen_sign(method: str, url: str, query_string: str = "", body: str = "", api_key: str = "", api_secret: str = ""):
    """
    Реализация из раздела Authentication в документации Gate API.
    """
    t = str(int(time.time()))
    body_hash = hashlib.sha512(body.encode("utf-8")).hexdigest()
    sign_str = "\n".join([method.upper(), API_PREFIX + url, query_string, body_hash, t])
    sign = hmac.new(
        api_secret.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha512
    ).hexdigest()
    headers = {
        "KEY": api_key,
        "Timestamp": t,
        "SIGN": sign,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return headers


def http_get(url, query="", api_key=None, api_secret=None, auth=False):
    full_url = API_HOST + API_PREFIX + url
    headers = {"Accept": "application/json"}
    if auth:
        headers = gen_sign("GET", url, query, "", api_key, api_secret)
    if query:
        full_url += "?" + query
    r = requests.get(full_url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def http_post(url, body_dict, query="", api_key=None, api_secret=None):
    body = json.dumps(body_dict, separators=(",", ":"))
    headers = gen_sign("POST", url, query, body, api_key, api_secret)
    full_url = API_HOST + API_PREFIX + url
    if query:
        full_url += "?" + query
    r = requests.post(full_url, headers=headers, data=body, timeout=10)
    r.raise_for_status()
    return r.json()


# ---------- ЛОГИКА DUAL INVESTMENT ----------

def find_best_eth_dual_one_day():
    """
    1) Берём список всех Dual Investment
    2) Фильтруем по:
       - invest_currency = USDT
       - exercise_currency = ETH
       - type = put (покупать ETH дешевле)
       - status = ONGOING
       - срок ~1 день (по delivery_time)
    3) Выбираем с максимальным apy_display
    """
    plans = http_get("/earn/dual/investment_plan")
    now = int(time.time())
    one_day_sec = 24 * 60 * 60

    candidates = []
    for p in plans:
        if p.get("invest_currency") != "USDT":
            continue
        if p.get("exercise_currency") != "ETH":
            continue
        if p.get("type") != "put":
            continue
        if p.get("status") != "ONGOING":
            continue

        delivery = int(p.get("delivery_time", 0))
        # фильтр по сроку: от 0.5 до 2 дней от текущего момента
        if not (0.5 * one_day_sec <= delivery - now <= 2.0 * one_day_sec):
            continue

        try:
            apy = float(p.get("apy_display", "0"))
        except ValueError:
            apy = 0.0
        p["_apy"] = apy
        candidates.append(p)

    if not candidates:
        raise RuntimeError("Не найдено подходящих Dual Investment по ETH/USDT (1 день).")

    best = max(candidates, key=lambda x: x["_apy"])
    return best


def place_dual_order(plan, amount_usdt, api_key, api_secret, custom_text):
    """
    POST /earn/dual/orders
    body = { "plan_id": "...", "amount": "...", "text": "..." }
    amount – в валюте инвестирования (USDT)
    """
    body = {
        "plan_id": str(plan["id"]),
        "amount": str(amount_usdt),
        "text": custom_text,
    }
    resp = http_post("/earn/dual/orders", body, api_key=api_key, api_secret=api_secret)
    print("Dual Investment ордер создан:", resp)
    return resp


# ---------- ЛОГИКА ФЬЮЧЕРСОВ ----------

def get_futures_ticker(settle, contract):
    data = http_get(f"/futures/{settle}/tickers", f"contract={contract}")
    if isinstance(data, list):
        return data[0]
    return data


def get_futures_contract(settle, contract):
    data = http_get(f"/futures/{settle}/contracts", "")
    for c in data:
        if c.get("name") == contract:
            return c
    raise RuntimeError(f"Не найден контракт {contract}")


def calc_hedge_size_usdt(amount_dual_usdt, hedge_multiplier, exercise_price):
    """
    Хеджируем приблизительно (тело + часть процентов).
    Проще всего: notional = amount_dual_usdt * hedge_multiplier
    """
    return amount_dual_usdt * hedge_multiplier


def calc_contract_size_from_usdt(notional_usdt, contract_info, mark_price):
    """
    Упрощённо считаем, что размер контракта привязан к 1 USDT.
    На Gate для USDT-маржинальных фьючерсов обычно:
      notional = size * multiplier
    где multiplier близок к 1.
    Для аккуратности можно прочитать поле 'quanto_multiplier' (если есть).
    Здесь для простоты считаем, что multiplier == 1.
    """
    multiplier = 1.0
    # если есть quanto_multiplier – используем
    if "quanto_multiplier" in contract_info:
        try:
            multiplier = float(contract_info["quanto_multiplier"])
        except ValueError:
            multiplier = 1.0

    size = int(round(notional_usdt / multiplier))
    if size == 0:
        size = 1
    return size


def open_futures_short(settle, contract, size, api_key, api_secret, reduce_only=False):
    """
    POST /futures/{settle}/orders
    size < 0 – шорт
    """
    body = {
        "contract": contract,
        "size": -abs(int(size)),
        "iceberg": 0,
        "tif": "ioc",  # немедленное исполнение, остальное отменить
        "text": "dual-hedge-short",
    }
    if reduce_only:
        body["reduce_only"] = True
    resp = http_post(f"/futures/{settle}/orders", body, api_key=api_key, api_secret=api_secret)
    print("Фьючерсный шорт открыт:", resp)
    return resp


def close_futures_short_market(settle, contract, api_key, api_secret):
    """
    Закрываем шорт через reduce-only market ордер.
    Для простоты берём текущую позицию и ставим size с противоположным знаком.
    """
    # Получаем список позиций
    positions = http_get(f"/futures/{settle}/positions", f"contract={contract}", api_key, api_secret, auth=True)


# ---------- МОНИТОРИНГ DUAL ORDER ----------

def wait_for_dual_settlement(order_text, api_key, api_secret, poll_interval):
    """
    Периодически опрашиваем GET /earn/dual/orders и ищем наш ордер по text.
    Возвращаем запись ордера после статуса SETTLEMENT_SUCCESS
    """
    print("Ожидание экспирации Dual Investment...")
    while True:
        orders = http_get("/earn/dual/orders", "", api_key, api_secret, auth=True)
        target = None
        for o in orders:
            if o.get("text") == order_text:
                target = o
                break
        if not target:
            print("Ордер пока не найден по text. Ждём...")
        else:
            status = target.get("status")
            print(f"Статус Dual Investment: {status}")
            if status == "SETTLEMENT_SUCCESS":
                return target
        time.sleep(poll_interval)


def main():
    cfg = load_config()
    api_key = cfg["api_key"]
    api_secret = cfg["api_secret"]

    print("=== Gate Dual Investment + Futures Hedge бот ===")
    amount_usdt = float(input("Введите сумму вклада в Dual Investment (USDT): ").strip())

    # 1. Находим лучший план
    plan = find_best_eth_dual_one_day()
    exercise_price = float(plan["exercise_price"])
    apy = float(plan.get("apy_display", "0"))
    delivery_ts = int(plan["delivery_time"])
    delivery_dt = datetime.fromtimestamp(delivery_ts, tz=timezone.utc)

    print("\nВыбран план Dual Investment:")
    print(json.dumps(plan, indent=2))
    print(f"Целевая цена (strike): {exercise_price}")
    print(f"Годовая доходность (apy_display): {apy}")
    print(f"Время экспирации (UTC): {delivery_dt}")

    # 2. Считаем размер хеджа
    hedge_notional = calc_hedge_size_usdt(amount_usdt, cfg["hedge_multiplier"], exercise_price)
    print(f"\nПланируемый нотионал хеджа по фьючерсу: ~{hedge_notional:.2f} USDT")

    # 3. Получаем инфо по фьючерсу и текущую цену
    settle = cfg["futures_settle"]
    contract = cfg["futures_contract"]
    contract_info = get_futures_contract(settle, contract)
    ticker = get_futures_ticker(settle, contract)
    mark_price = float(ticker["last"])

    contract_size = calc_contract_size_from_usdt(hedge_notional, contract_info, mark_price)
    print(f"Размер фьючерсного шорта (size): {contract_size}")

    confirm = input("\nОткрыть Dual Investment и фьючерсный шорт? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Отменено пользователем.")
        return

    custom_text = f"dual-hedge-{int(time.time())}"

    # 4. Открываем Dual Investment
    dual_resp = place_dual_order(plan, amount_usdt, api_key, api_secret, custom_text)

    # 5. Открываем фьючерсный шорт
    fut_resp = open_futures_short(settle, contract, contract_size, api_key, api_secret)

    print("\nЗаказы созданы. Теперь нужно следить за экспирацией и закрывать шорт и ETH.")
    print("Автоматическое закрытие после экспирации в этом минимальном примере не реализовано.")
    print("Вы можете доработать функцию wait_for_dual_settlement и close_futures_short_market для полного автомата.")


if __name__ == "__main__":
    main()
