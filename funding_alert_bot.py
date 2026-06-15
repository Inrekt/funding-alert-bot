"""
Telegram-бот: алерты по funding rate на фьючерсах.
Версия для запуска по расписанию через GitHub Actions (каждые 5 минут).

Функции:
- Мониторинг 9 бирж: Binance, Bybit, OKX, Gate, Bitget, MEXC, BingX, Kucoin, Hyperliquid
- Алерт когда funding <= -2% (первичный)
- Обновление каждые 5 минут пока funding держится ниже -2%
- Алерт о восстановлении когда funding вернулся выше -2%
- В каждом сообщении: таблица по биржам + памп/дамп за 24ч + движение от лоя/ATH + объём + OI
"""

import os
import json
import time
import ccxt
import requests

# ===================== НАСТРОЙКИ =====================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

FUNDING_THRESHOLD = -0.02        # -2%
UPDATE_INTERVAL_MIN = 5          # обновление каждые 5 минут пока funding ниже порога
STATE_FILE = "state.json"

EXCHANGES = [
    "binance",
    "bybit",
    "okx",
    "gate",
    "bitget",
    "mexc",
    "bingx",
    "kucoin",
    "hyperliquid",
]

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
    if not funding_timestamp_ms:
        return "--:--:--"
    now_ms = time.time() * 1000
    diff = max(0, funding_timestamp_ms - now_ms)
    diff_sec = int(diff / 1000)
    h = diff_sec // 3600
    m = (diff_sec % 3600) // 60
    s = diff_sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_base_symbol(symbol: str) -> str:
    """Извлекает базовый тикер из символа типа 'BTC/USDT:USDT' -> 'BTC'"""
    return symbol.split("/")[0]


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
                print(f"[{exchange_id}] fetchFundingRates не поддерживается")
                continue

            exchange.load_markets()
            all_data[exchange_id] = exchange.fetch_funding_rates()
            print(f"[{exchange_id}] получено {len(all_data[exchange_id])} пар")

        except Exception as e:
            print(f"[{exchange_id}] ошибка: {e}")

    return all_data


def fetch_ticker_data(exchange_id: str, symbol: str) -> dict:
    """
    Получает расширенные данные по тикеру: цена, объём 24ч, high/low 24ч, OI.
    Возвращает словарь с данными или пустой словарь при ошибке.
    """
    try:
        exchange_class = getattr(ccxt, exchange_id)
        exchange = exchange_class({"enableRateLimit": True})
        if "defaultType" in exchange.options:
            exchange.options["defaultType"] = "swap"
        exchange.load_markets()

        result = {}

        # Тикер: цена, объём, high/low за 24ч
        if exchange.has.get("fetchTicker"):
            ticker = exchange.fetch_ticker(symbol)
            result["price"] = ticker.get("last") or ticker.get("close")
            result["volume_24h"] = ticker.get("quoteVolume") or ticker.get("baseVolume")
            result["high_24h"] = ticker.get("high")
            result["low_24h"] = ticker.get("low")
            result["change_24h_pct"] = ticker.get("percentage")  # % изменения за 24ч

        # Open Interest
        if exchange.has.get("fetchOpenInterest"):
            try:
                oi = exchange.fetch_open_interest(symbol)
                result["open_interest"] = oi.get("openInterestValue") or oi.get("openInterest")
            except Exception:
                pass

        return result

    except Exception as e:
        print(f"[{exchange_id}] ошибка ticker для {symbol}: {e}")
        return {}


def aggregate_market_data(all_data: dict, symbol: str) -> dict:
    """
    Агрегирует рыночные данные по символу со всех бирж.
    Берёт среднюю цену, суммирует объёмы, берёт абсолютный high/low.
    """
    prices = []
    volumes = []
    highs = []
    lows = []
    changes = []
    oi_total = 0

    # Берём данные с биржи где сработал алерт (там уже есть markPrice)
    for exchange_id, rates in all_data.items():
        data = rates.get(symbol, {})
        if not data:
            continue

        price = data.get("markPrice") or data.get("indexPrice")
        if price:
            prices.append(price)

    # Дополнительно тянем тикер с Binance (самые надёжные данные по high/low/volume)
    # и с других бирж где есть монета
    for exchange_id in ["binance", "bybit", "gate"]:
        if symbol in all_data.get(exchange_id, {}):
            ticker = fetch_ticker_data(exchange_id, symbol)
            if ticker.get("price"):
                prices.append(ticker["price"])
            if ticker.get("volume_24h"):
                volumes.append(ticker["volume_24h"])
            if ticker.get("high_24h"):
                highs.append(ticker["high_24h"])
            if ticker.get("low_24h"):
                lows.append(ticker["low_24h"])
            if ticker.get("change_24h_pct") is not None:
                changes.append(ticker["change_24h_pct"])
            if ticker.get("open_interest"):
                oi_total += ticker["open_interest"]

    result = {}
    if prices:
        result["avg_price"] = sum(prices) / len(prices)
    if volumes:
        result["total_volume_24h"] = sum(volumes)
    if highs:
        result["high_24h"] = max(highs)
    if lows:
        result["low_24h"] = min(lows)
    if changes:
        result["change_24h_pct"] = sum(changes) / len(changes)
    if oi_total:
        result["open_interest"] = oi_total

    return result


def format_number(n, decimals=2) -> str:
    """Форматирует число с суффиксами K/M/B для больших значений."""
    if n is None:
        return "N/A"
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.{decimals}f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.{decimals}f}M"
    if n >= 1_000:
        return f"{n/1_000:.{decimals}f}K"
    return f"{n:.{decimals}f}"


def build_comparison_table(all_data: dict, symbol: str) -> str:
    """Строит таблицу сравнения funding rate по всем биржам для данной пары."""
    header = f"{'Exc.':<12}| {'Price':<10}| {'Funding':<10}| Countdown"
    lines = [header]

    rows = []
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

        rows.append((rate or 0, f"{exchange_id.upper():<12}| {price_str:<10}| {rate_str:<10}| {countdown}"))

    # Сортируем по funding rate (самые низкие сверху)
    rows.sort(key=lambda x: x[0])
    lines.extend(row[1] for row in rows)

    return "\n".join(lines)


def build_market_summary(market: dict, symbol: str) -> str:
    """Строит блок с рыночной статистикой."""
    lines = []
    base = get_base_symbol(symbol)

    avg_price = market.get("avg_price")
    change = market.get("change_24h_pct")
    high = market.get("high_24h")
    low = market.get("low_24h")
    volume = market.get("total_volume_24h")
    oi = market.get("open_interest")

    if avg_price:
        lines.append(f"💰 Цена: ${format_number(avg_price, 4)}")

    if change is not None:
        emoji = "📈" if change >= 0 else "📉"
        direction = "памп" if change >= 0 else "дамп"
        lines.append(f"{emoji} 24ч {direction}: {change:+.2f}%")

    if high and low and avg_price:
        # Движение от лоя (если памп) или от хая (если дамп)
        if change is not None and change >= 0:
            from_low = ((avg_price - low) / low * 100) if low > 0 else 0
            lines.append(f"🚀 От лоя 24ч: +{from_low:.2f}%")
        else:
            from_high = ((high - avg_price) / high * 100) if high > 0 else 0
            lines.append(f"🔻 От хая 24ч: -{from_high:.2f}%")

        lines.append(f"📊 24ч диапазон: ${format_number(low, 4)} — ${format_number(high, 4)}")

    if volume:
        lines.append(f"💹 Объём 24ч: ${format_number(volume)}")

    if oi:
        lines.append(f"📐 Open Interest: ${format_number(oi)}")

    return "\n".join(lines)


def build_alert_message(
    symbol: str,
    trigger_exchange: str,
    trigger_rate: float,
    all_data: dict,
    market: dict,
    alert_type: str,  # "new", "update", "recovery"
    minutes_below: int = 0,
) -> str:

    table = build_comparison_table(all_data, symbol)
    market_info = build_market_summary(market, symbol)

    if alert_type == "new":
        header = (
            f"🔻 *Funding rate упал ниже -2%*\n"
            f"Биржа: {trigger_exchange} | Пара: {symbol}\n"
            f"Funding: {trigger_rate * 100:.3f}%"
        )
    elif alert_type == "update":
        header = (
            f"🔄 *Обновление* | {symbol}\n"
            f"Funding держится ниже -2% уже {minutes_below} мин\n"
            f"Биржа: {trigger_exchange} | {trigger_rate * 100:.3f}%"
        )
    else:  # recovery
        header = (
            f"✅ *Funding вернулся в норму*\n"
            f"Биржа: {trigger_exchange} | Пара: {symbol}\n"
            f"Текущий funding: {trigger_rate * 100:.3f}%"
        )

    return f"{header}\n\n{market_info}\n\n```\n{table}\n```"


def main():
    state = load_state()
    all_data = fetch_all_funding()
    now_ts = int(time.time())

    for exchange_id, funding_rates in all_data.items():
        for symbol, data in funding_rates.items():
            rate = data.get("fundingRate")
            if rate is None:
                continue

            key = f"{exchange_id}:{symbol}"
            prev = state.get(key, {})
            # prev структура: {"below": bool, "first_ts": int, "last_update_ts": int}

            was_below = prev.get("below", False)
            is_below = rate <= FUNDING_THRESHOLD

            if is_below and not was_below:
                # Новый алерт — funding только что упал ниже порога
                market = aggregate_market_data(all_data, symbol)
                msg = build_alert_message(symbol, exchange_id, rate, all_data, market, "new")
                send_telegram_message(msg)
                state[key] = {
                    "below": True,
                    "first_ts": now_ts,
                    "last_update_ts": now_ts,
                }

            elif is_below and was_below:
                # Уже был ниже — проверяем, пора ли слать обновление (каждые 5 минут)
                last_update = prev.get("last_update_ts", now_ts)
                first_ts = prev.get("first_ts", now_ts)
                minutes_below = (now_ts - first_ts) // 60

                if now_ts - last_update >= UPDATE_INTERVAL_MIN * 60:
                    market = aggregate_market_data(all_data, symbol)
                    msg = build_alert_message(
                        symbol, exchange_id, rate, all_data, market, "update", minutes_below
                    )
                    send_telegram_message(msg)
                    state[key] = {
                        "below": True,
                        "first_ts": first_ts,
                        "last_update_ts": now_ts,
                    }

            elif not is_below and was_below:
                # Funding вернулся выше порога
                market = aggregate_market_data(all_data, symbol)
                msg = build_alert_message(symbol, exchange_id, rate, all_data, market, "recovery")
                send_telegram_message(msg)
                state[key] = {"below": False}

    save_state(state)
    print("Проверка завершена.")


if __name__ == "__main__":
    main()
