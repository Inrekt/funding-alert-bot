"""
Telegram-бот: алерты по funding rate на фьючерсах (perpetual swaps).
Версия для запуска по расписанию через GitHub Actions.

Что делает за ОДИН запуск:
- опрашивает указанные биржи, получает funding rate по ВСЕМ парам сразу
- сравнивает с предыдущим состоянием (хранится в state.json)
- если ставка <= FUNDING_THRESHOLD — присылает алерт "упал ниже порога"
  + таблицу с ценой/funding/обратным отсчётом по этой же монете на всех биржах
- если ставка вернулась выше FUNDING_THRESHOLD — присылает алерт "вернулся в норму"
- сохраняет новое состояние в state.json
"""

import os
import json
import time
import ccxt
import requests

# ===================== НАСТРОЙКИ =====================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

FUNDING_THRESHOLD = -0.02   # -2% (ccxt отдаёт funding rate как долю)
STATE_FILE = "state.json"

# Какие биржи мониторить (названия как в ccxt: https://docs.ccxt.com/#/exchanges)
EXCHANGES = ["binance", "bybit", "okx", "gate", "bitget", "mexc"]

# ======================================================


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            },
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


def format_countdown(funding_timestamp_ms):
    """Возвращает строку HH:MM:SS до момента выплаты funding."""
    if not funding_timestamp_ms:
        return "--:--:--"
    now_ms = time.time() * 1000
    diff = max(0, funding_timestamp_ms - now_ms)
    diff_sec = int(diff / 1000)
    h = diff_sec // 3600
    m = (diff_sec % 3600) // 60
    s = diff_sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def fetch_all_funding() -> dict:
    """Возвращает {exchange_id: {symbol: data}} для всех бирж."""
    all_data = {}
    for exchange_id in EXCHANGES:
        try:
            exchange_class = getattr(ccxt, exchange_id)
            exchange = exchange_class({"enableRateLimit": True})

            if "defaultType" in exchange.options:
                exchange.options["defaultType"] = "swap"

            if not exchange.has.get("fetchFundingRates"):
                print(f"[{exchange_id}] fetchFundingRates не поддерживается, пропускаю")
                continue

            exchange.load_markets()
            all_data[exchange_id] = exchange.fetch_funding_rates()

        except Exception as e:
            print(f"[{exchange_id}] ошибка при получении данных: {e}")

    return all_data


def build_comparison_table(all_data: dict, symbol: str) -> str:
    """Строит таблицу сравнения funding rate по всем биржам для данной пары."""
    header = f"{'Exc.':<9}| {'Price':<10}| {'Funding':<9}| Countdown"
    lines = [header]

    for exchange_id in EXCHANGES:
        data = all_data.get(exchange_id, {}).get(symbol)
        if not data:
            continue

        rate = data.get("fundingRate")
        price = data.get("markPrice") or data.get("indexPrice") or data.get("lastPrice")
        funding_ts = data.get("fundingTimestamp")

        rate_str = f"{rate * 100:.4f}%" if rate is not None else "N/A"
        price_str = f"{price:.6f}" if isinstance(price, (int, float)) else "N/A"
        countdown = format_countdown(funding_ts)

        lines.append(f"{exchange_id.upper():<9}| {price_str:<10}| {rate_str:<9}| {countdown}")

    return "\n".join(lines)


def main():
    state = load_state()
    all_data = fetch_all_funding()

    for exchange_id, funding_rates in all_data.items():
        for symbol, data in funding_rates.items():
            rate = data.get("fundingRate")
            if rate is None:
                continue

            key = f"{exchange_id}:{symbol}"
            was_below = state.get(key, False)
            is_below = rate <= FUNDING_THRESHOLD

            if is_below and not was_below:
                table = build_comparison_table(all_data, symbol)
                msg = (
                    f"🔻 Funding rate упал ниже порога: {rate * 100:.3f}%\n"
                    f"Биржа: {exchange_id}\n"
                    f"Пара: {symbol}\n\n"
                    f"```\n{table}\n```"
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

    save_state(state)
    print("Проверка завершена.")


if __name__ == "__main__":
    main()
