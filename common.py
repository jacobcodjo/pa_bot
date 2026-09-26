"""Primitives communes aux deux stratégies (Impulse et CRT)."""

from datetime import datetime, timezone

from data_client import is_forex_or_gold

GRANULARITY_SECONDS = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900}


def has_weekend_gaps(symbol):
    return is_forex_or_gold(symbol)  # la crypto trade 24/7, pas le forex/or


def is_gap_candle(candles, index, granularity_seconds, gap_multiplier=1.5):
    if index == 0 or not granularity_seconds:
        return True
    return (candles[index]["epoch"] - candles[index - 1]["epoch"]) > granularity_seconds * gap_multiplier


def find_swing_highs_lows(candles, left=3, right=3):
    """Détection de swing points (pivots) sur une série de bougies."""
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        window = candles[i - left:i + right + 1]
        if candles[i]["high"] == max(c["high"] for c in window):
            highs.append(i)
        if candles[i]["low"] == min(c["low"] for c in window):
            lows.append(i)
    return highs, lows


def detect_trend(candles, swing_window, swing_count, sma_period):
    """
    Détecte la tendance de fond : combine une structure de swing (swing_count
    derniers highs ET lows tous croissants/décroissants) et une confirmation
    par SMA. Retourne "bullish", "bearish", "range" ou None.
    """
    closed = candles[:-1] if len(candles) > 1 else candles
    if len(closed) < sma_period:
        return None

    highs, lows = find_swing_highs_lows(closed, swing_window, swing_window)
    if len(highs) < swing_count or len(lows) < swing_count:
        return None

    recent_highs = [closed[i]["high"] for i in highs[-swing_count:]]
    recent_lows = [closed[i]["low"] for i in lows[-swing_count:]]

    structure_bullish = (
        all(recent_highs[i] < recent_highs[i + 1] for i in range(len(recent_highs) - 1))
        and all(recent_lows[i] < recent_lows[i + 1] for i in range(len(recent_lows) - 1))
    )
    structure_bearish = (
        all(recent_highs[i] > recent_highs[i + 1] for i in range(len(recent_highs) - 1))
        and all(recent_lows[i] > recent_lows[i + 1] for i in range(len(recent_lows) - 1))
    )

    sma = sum(c["close"] for c in closed[-sma_period:]) / sma_period
    last_close = closed[-1]["close"]

    if structure_bullish and last_close > sma:
        return "bullish"
    if structure_bearish and last_close < sma:
        return "bearish"
    return "range"


def aggregate_to_weekly(d1_candles):
    """Agrège des bougies D1 en bougies W1 (une par semaine calendaire ISO)."""
    if not d1_candles:
        return []
    weeks = {}
    for c in d1_candles:
        dt = datetime.fromtimestamp(c["epoch"], tz=timezone.utc)
        key = dt.isocalendar()[:2]
        weeks.setdefault(key, []).append(c)

    weekly = []
    for key in sorted(weeks.keys()):
        group = weeks[key]
        weekly.append({
            "epoch": group[-1]["epoch"],
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"],
        })
    return weekly


def find_index_after_epoch(candles, epoch):
    for i, c in enumerate(candles):
        if c["epoch"] > epoch:
            return i
    return None


def detect_structure_shift(candles, start_index, direction, left=2, right=2):
    """Cherche, à partir de start_index, une cassure de structure dans le sens donné."""
    highs, lows = find_swing_highs_lows(candles[:start_index + 1], left, right)

    if direction == "bullish":
        if not highs:
            return None
        pivot_index = highs[-1]
        pivot_level = candles[pivot_index]["high"]
        for j in range(start_index + 1, len(candles)):
            if candles[j]["close"] > pivot_level:
                return {"break_index": j, "level": pivot_level, "pivot_index": pivot_index}
    else:
        if not lows:
            return None
        pivot_index = lows[-1]
        pivot_level = candles[pivot_index]["low"]
        for j in range(start_index + 1, len(candles)):
            if candles[j]["close"] < pivot_level:
                return {"break_index": j, "level": pivot_level, "pivot_index": pivot_index}
    return None


def classify_order_type(direction, entry, current_price, tolerance_pct=0.0005):
    if entry is None or not current_price:
        return "Buy" if direction == "bullish" else "Sell"
    diff_pct = abs(current_price - entry) / current_price
    if diff_pct <= tolerance_pct:
        return "Buy" if direction == "bullish" else "Sell"
    if direction == "bullish":
        return "Buy Limit" if entry < current_price else "Buy Stop"
    else:
        return "Sell Limit" if entry > current_price else "Sell Stop"
