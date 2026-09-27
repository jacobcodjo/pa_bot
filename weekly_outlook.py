"""
Outlook hebdomadaire : pendant la fermeture du week-end, détermine un biais
directionnel global (D1 et W1) pour chaque actif -- forex/or (dernière
clôture connue, marché fermé) et crypto (qui continue de trader
normalement). Envoyé une seule fois par week-end via un marqueur persistant
(weekly_outlook_state.json), pas à chaque passage du scan.

Réutilise la même détection de tendance que les deux stratégies (common.py)
-- ce n'est donc pas une troisième stratégie, juste une lecture de la
tendance de fond, sans setup d'entrée/sortie associé.
"""

import json
import os
from datetime import datetime, timezone

from common import detect_trend, aggregate_to_weekly, has_weekend_gaps, is_gap_candle, GRANULARITY_SECONDS
from config import (
    ALL_SYMBOLS, GOLD, CRYPTOS, CANDLE_COUNT,
    TREND_SWING_WINDOW, TREND_SWING_COUNT, TREND_SMA_PERIOD,
    WEEKLY_OUTLOOK_FILE,
)
from data_client import get_many_candles
from sessions import is_forex_market_open, display_time
from notifier import send_telegram_message, display_symbol

TREND_LABEL = {
    "bullish": "🟢 Haussier",
    "bearish": "🔴 Baissier",
    "range": "⚪ Range",
    None: "⚪ Indéterminé",
}


def load_marker() -> dict:
    if not os.path.exists(WEEKLY_OUTLOOK_FILE):
        return {}
    try:
        with open(WEEKLY_OUTLOOK_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_marker(marker: dict):
    with open(WEEKLY_OUTLOOK_FILE, "w") as f:
        json.dump(marker, f)


def _week_key(now_utc: datetime) -> str:
    year, week, _ = now_utc.isocalendar()
    return f"{year}-W{week:02d}"


def should_send(now_utc: datetime = None) -> bool:
    """
    True seulement pendant la fermeture week-end du forex, et seulement si
    l'outlook n'a pas déjà été envoyé pour cette semaine ISO (vendredi,
    samedi et dimanche d'un même week-end tombent toujours sur la même
    semaine ISO -- pas de risque de double-déclenchement autour de minuit).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    if is_forex_market_open(now_utc):
        return False
    marker = load_marker()
    return marker.get("last_sent_week") != _week_key(now_utc)


def should_send_crypto_refresh(now_utc: datetime = None) -> bool:
    """
    Rafraîchissement crypto uniquement, le dimanche en fin d'après-midi
    (20h00 UTC), avant la réouverture du forex (~22h00 UTC) -- la lecture du
    vendredi soir devient de moins en moins pertinente pour la crypto au fil
    du week-end puisqu'elle continue de trader, contrairement au forex qui
    reste figé sur la même clôture jusqu'à lundi.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    if is_forex_market_open(now_utc):
        return False
    if not (now_utc.weekday() == 6 and now_utc.hour >= 20):  # Dimanche >= 20h00 UTC
        return False
    marker = load_marker()
    return marker.get("last_sent_crypto_refresh_week") != _week_key(now_utc)


def _symbol_trend(symbol: str, d1_candles: list) -> dict:
    if has_weekend_gaps(symbol):
        clean = [
            c for i, c in enumerate(d1_candles)
            if not is_gap_candle(d1_candles, i, GRANULARITY_SECONDS["D1"])
        ]
    else:
        clean = d1_candles

    d1_trend = detect_trend(clean, TREND_SWING_WINDOW, TREND_SWING_COUNT, TREND_SMA_PERIOD)
    weekly = aggregate_to_weekly(clean)
    w1_trend = detect_trend(weekly, TREND_SWING_WINDOW, TREND_SWING_COUNT, TREND_SMA_PERIOD)
    return {"d1": d1_trend, "w1": w1_trend}


def _build_trend_lines(symbols: list) -> dict:
    """Récupère le D1 et calcule le biais D1/W1 pour chaque symbole donné."""
    specs = [(symbol, "D1", CANDLE_COUNT) for symbol in symbols]
    results = get_many_candles(specs)

    lines_by_group = {"Forex": [], "Or": [], "Crypto": []}
    for symbol in symbols:
        candles = results.get((symbol, "D1"))
        if not candles or isinstance(candles, Exception):
            print(f"[outlook/{symbol}] Pas de données D1 -- ignoré.")
            continue

        trend = _symbol_trend(symbol, candles)
        line = f"{display_symbol(symbol)} : {TREND_LABEL[trend['d1']]} (D1) / {TREND_LABEL[trend['w1']]} (W1)"

        if symbol in GOLD:
            lines_by_group["Or"].append(line)
        elif symbol in CRYPTOS:
            lines_by_group["Crypto"].append(line)
        else:
            lines_by_group["Forex"].append(line)

    return lines_by_group


def build_and_send(now_utc: datetime = None):
    now_utc = now_utc or datetime.now(timezone.utc)

    # Le D1 est souvent déjà en cache (fetché pendant la semaine par le scan
    # normal), donc ce passage ne coûte généralement que peu ou pas de
    # nouvelles requêtes API.
    lines_by_group = _build_trend_lines(ALL_SYMBOLS)

    if not any(lines_by_group.values()):
        print("Outlook hebdomadaire : aucune donnée disponible, envoi annulé.")
        return

    parts = [f"📅 <b>Outlook de la semaine</b> — {display_time(now_utc)}"]
    for group in ("Forex", "Or", "Crypto"):
        if lines_by_group[group]:
            parts.append(f"\n<b>{group}</b>")
            parts.extend(lines_by_group[group])

    send_telegram_message("\n".join(parts))
    print("Outlook hebdomadaire envoyé.")

    marker = load_marker()
    marker["last_sent_week"] = _week_key(now_utc)
    save_marker(marker)


def build_and_send_crypto_refresh(now_utc: datetime = None):
    now_utc = now_utc or datetime.now(timezone.utc)

    lines_by_group = _build_trend_lines(CRYPTOS)
    crypto_lines = lines_by_group["Crypto"]

    if not crypto_lines:
        print("Rafraîchissement crypto du dimanche : aucune donnée disponible, envoi annulé.")
        return

    parts = [f"🔄 <b>Mise à jour crypto</b> (dimanche) — {display_time(now_utc)}", ""]
    parts.extend(crypto_lines)

    send_telegram_message("\n".join(parts))
    print("Rafraîchissement crypto du dimanche envoyé.")

    marker = load_marker()
    marker["last_sent_crypto_refresh_week"] = _week_key(now_utc)
    save_marker(marker)
