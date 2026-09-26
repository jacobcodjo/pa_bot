import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from sessions import display_time


def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "PA_TELEGRAM_BOT_TOKEN ou PA_TELEGRAM_CHAT_ID manquant (variables d'environnement)."
        )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    return response.json()


def display_symbol(symbol: str) -> str:
    return symbol.replace("_", "/")  # EUR_USD -> EUR/USD, BTCUSDT reste tel quel


def format_number(value, max_decimals):
    if value is None:
        return None
    rounded = round(value, max_decimals)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.{max_decimals}f}".rstrip("0").rstrip(".")


def format_setup_message(setup: dict, sessions: list) -> str:
    direction_emoji = "🟢" if setup["direction"] == "bullish" else "🔴"
    order_type = setup.get("order_type", "").upper()
    decimals = 2 if "JPY" in setup["symbol"] or setup["symbol"].endswith("USDT") else 4

    entry = setup["entry"]
    sl = setup["stop_loss"]
    tp1 = setup["take_profit_1"]
    tp2 = setup["take_profit_2"]

    while decimals < 10:
        rounded_values = [round(v, decimals) for v in (entry, sl, tp1, tp2) if v is not None]
        if len(rounded_values) == len(set(rounded_values)):
            break
        decimals += 1

    session_label = "/".join(s.replace("_", " ").title() for s in sessions) if sessions else "?"
    strategy = setup.get("strategy", "?")

    trend = setup.get("trend")
    counter_trend = setup.get("counter_trend")
    if trend in ("bullish", "bearish") and counter_trend is not None:
        trend_line = "⚠️ Contre-tendance" if counter_trend else "✅ Sens de la tendance"
    else:
        trend_line = None

    lines = [
        f"{direction_emoji} <b>{display_symbol(setup['symbol'])}</b> — {session_label}",
        f"Stratégie : <b>{strategy}</b>",
        f"{order_type} ({setup.get('trigger_tf','?')})",
        f"Entrée: {format_number(entry, decimals)}",
        f"SL: {format_number(sl, decimals)}",
        f"TP1: {format_number(tp1, decimals)}",
        f"TP2: {format_number(tp2, decimals)}",
    ]

    if trend_line:
        lines.append(trend_line)

    if setup.get("risk_reward_2") or setup.get("risk_reward_1"):
        rr = setup.get("risk_reward_2") or setup.get("risk_reward_1")
        lines.append(f"RR: 1:{rr}")

    lines.append(f"Heure : {display_time()}")

    return "\n".join(lines)
