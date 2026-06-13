"""
Telegram-бот: алерты по funding rate на фьючерсах (perpetual swaps).
Версия для запуска по расписанию через GitHub Actions.

Что делает за ОДИН запуск:
- опрашивает указанные биржи
- получает funding rate по ВСЕМ доступным монетам сразу (через ccxt.fetch_funding_rates)
- сравнивает с предыдущим состоянием (хранится в state.json)
- если ставка <= FUNDING_THRESHOLD — присылает алерт "упал ниже порога"
- если ставка вернулась выше FUNDING_THRESHOLD — присылает алерт "вернулся в норму"
- сохраняет новое состояние в state.json (GitHub Actions сам закоммитит этот файл обратно)

Токен и chat_id берутся из переменных окружения (секретов GitHub Actions),
а не хранятся в коде.
"""

import os
import json
import ccxt
import requests

# ===================== НАСТРОЙКИ =====================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

FUNDING_THRESHOLD = -0.002   # -2% (ccxt отдаёт funding rate как долю)
STATE_FILE = "state.json"

# Какие биржи мониторить (названия как в ccxt: https://docs.ccxt.com/#/exchanges)
EXCHANGES = ["binance", "bybit", "okx", "gate", "bitget", "mexc"]

# ======================================================


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"Telegram API ошибка: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Не удалось отправить сообщение в Telegram: {e}")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def check_exchange(exchange_id: str, state: dict):
    try:
        exchange_class = getattr(ccxt, exchange_id)
        exchange = exchange_class({"enableRateLimit": True})

        if "defaultType" in exchange.options:
            exchange.options["defaultType"] = "swap"

        if not exchange.has.get("fetchFundingRates"):
            print(f"[{exchange_id}] fetchFundingRates не поддерживается, пропускаю")
            return

        exchange.load_markets()
        funding_rates = exchange.fetch_funding_rates()

        for symbol, data in funding_rates.items():
            rate = data.get("fundingRate")
            if rate is None:
                continue

            key = f"{exchange_id}:{symbol}"
            was_below = state.get(key, False)
            is_below = rate <= FUNDING_THRESHOLD

            if is_below and not was_below:
                msg = (
                    f"🔻 Funding rate упал ниже порога: {rate * 100:.3f}%\n"
                    f"Биржа: {exchange_id}\n"
                    f"Пара: {symbol}"
                )
                print(msg)
                send_telegram_message(msg)
                state[key] = True

            elif not is_below and was_below:
                msg = (
                    f"🔁 Funding rate вернулся в норму: {rate * 100:.3f}%\n"
                    f"Биржа: {exchange_id}\n"
                    f"Пара: {symbol}"
                )
                print(msg)
                send_telegram_message(msg)
                state[key] = False

    except Exception as e:
        print(f"[{exchange_id}] ошибка: {e}")


def main():
    state = load_state()

    for ex in EXCHANGES:
        check_exchange(ex, state)

    save_state(state)
    print("Проверка завершена.")


if __name__ == "__main__":
    main()
