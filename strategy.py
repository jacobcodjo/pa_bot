"""
Stratégie Impulsion/Correction + Fibonacci, exclusivement dans le sens de la
tendance, en cascade à 3 niveaux (HTF -> ITF -> Trigger) :

1. Tendance (HTF : W1, D1 ou H4) : 3 swings consécutifs + confirmation SMA.
2. Sur l'ITF associé, détection d'une jambe "impulsive" (nettement plus
   grande que la moyenne des jambes précédentes) allant DANS LE SENS de la
   tendance -- toute impulsion à contre-tendance est ignorée.
3. Attente d'une correction qui entre dans la zone Fibonacci 61.8%-79% de
   cette impulsion.
4. Sur le Trigger associé, recherche d'une cassure de structure dans le sens
   de la tendance -- le déclencheur final de l'alerte.
5. Entrée au milieu de la zone Fibonacci, stop au-delà de l'origine de
   l'impulsion, cibles aux extensions Fibonacci 127.2% (TP1) et 161.8% (TP2).

Implémentation simplifiée -- à backtester/affiner avant tout usage réel.
"""

from common import (
    has_weekend_gaps, is_gap_candle, find_swing_highs_lows, detect_trend,
    aggregate_to_weekly, find_index_after_epoch, detect_structure_shift,
    classify_order_type, GRANULARITY_SECONDS,
)
from config import (
    TREND_SWING_WINDOW, TREND_SWING_COUNT, TREND_SMA_PERIOD,
    IMPULSE_SWING_WINDOW, IMPULSE_LOOKBACK_LEGS, IMPULSE_SIZE_MULTIPLIER,
    FIB_RETRACEMENT_LOW, FIB_RETRACEMENT_HIGH,
    FIB_EXTENSION_TP1, FIB_EXTENSION_TP2,
    IMPULSE_STOP_LOSS_BUFFER_PCT, MIN_RISK_REWARD, ORDER_TYPE_TOLERANCE_PCT,
    TIMEFRAME_CASCADE_IMPULSE,
)


def find_legs(candles, swing_window=IMPULSE_SWING_WINDOW):
    """Découpe la série en jambes successives (d'un pivot au suivant)."""
    highs, lows = find_swing_highs_lows(candles, swing_window, swing_window)
    pivots = sorted(
        [(i, candles[i]["high"], "high") for i in highs]
        + [(i, candles[i]["low"], "low") for i in lows]
    )

    legs = []
    for j in range(1, len(pivots)):
        i_prev, price_prev, kind_prev = pivots[j - 1]
        i_cur, price_cur, kind_cur = pivots[j]
        if kind_prev == kind_cur:
            continue
        direction = "bullish" if price_cur > price_prev else "bearish"
        legs.append({
            "start_index": i_prev,
            "end_index": i_cur,
            "start_price": price_prev,
            "end_price": price_cur,
            "direction": direction,
            "size": abs(price_cur - price_prev),
        })
    return legs


def find_impulse_legs(candles, trend, swing_window=IMPULSE_SWING_WINDOW,
                       lookback_legs=IMPULSE_LOOKBACK_LEGS, size_multiplier=IMPULSE_SIZE_MULTIPLIER):
    legs = find_legs(candles, swing_window)
    impulses = []

    for idx, leg in enumerate(legs):
        if leg["direction"] != trend:
            continue
        prior = legs[max(0, idx - lookback_legs):idx]
        if len(prior) < 2:
            continue
        avg_size = sum(p["size"] for p in prior) / len(prior)
        if avg_size <= 0:
            continue
        if leg["size"] >= size_multiplier * avg_size:
            impulses.append(leg)

    return impulses


def compute_fib_zone(leg, level_low=FIB_RETRACEMENT_LOW, level_high=FIB_RETRACEMENT_HIGH):
    leg_range = leg["end_price"] - leg["start_price"]
    if leg["direction"] == "bullish":
        zone_high = leg["end_price"] - level_low * leg_range
        zone_low = leg["end_price"] - level_high * leg_range
    else:
        zone_low = leg["end_price"] - level_low * leg_range
        zone_high = leg["end_price"] - level_high * leg_range
    return zone_low, zone_high


def find_correction_touch(candles, leg, zone_low, zone_high):
    for i in range(leg["end_index"] + 1, len(candles)):
        c = candles[i]
        if c["low"] <= zone_high and c["high"] >= zone_low:
            return i
    return None


def compute_trade_levels(leg, zone_low, zone_high):
    entry = (zone_low + zone_high) / 2
    leg_range = leg["end_price"] - leg["start_price"]

    if leg["direction"] == "bullish":
        stop_loss = leg["start_price"] * (1 - IMPULSE_STOP_LOSS_BUFFER_PCT)
    else:
        stop_loss = leg["start_price"] * (1 + IMPULSE_STOP_LOSS_BUFFER_PCT)

    take_profit_1 = leg["start_price"] + FIB_EXTENSION_TP1 * leg_range
    take_profit_2 = leg["start_price"] + FIB_EXTENSION_TP2 * leg_range

    def calc_rr(target):
        risk = abs(entry - stop_loss)
        if risk <= 0:
            return None
        return round(abs(target - entry) / risk, 2)

    return {
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "risk_reward_1": calc_rr(take_profit_1),
        "risk_reward_2": calc_rr(take_profit_2),
    }


def analyze_symbol(symbol, candles_by_tf):
    setups = []
    check_gaps = has_weekend_gaps(symbol)

    for htf_tf, cascade in TIMEFRAME_CASCADE_IMPULSE.items():
        if htf_tf == "W1":
            trend_series = aggregate_to_weekly(candles_by_tf.get("D1"))
        else:
            trend_series = candles_by_tf.get(htf_tf)

        trend = (
            detect_trend(trend_series, TREND_SWING_WINDOW, TREND_SWING_COUNT, TREND_SMA_PERIOD)
            if trend_series else None
        )
        if trend not in ("bullish", "bearish"):
            continue

        itf_tf = cascade["itf"]
        itf_candles = candles_by_tf.get(itf_tf)
        if not itf_candles:
            continue

        if check_gaps:
            itf_clean = [
                c for i, c in enumerate(itf_candles)
                if not is_gap_candle(itf_candles, i, GRANULARITY_SECONDS[itf_tf])
            ]
        else:
            itf_clean = itf_candles

        impulses = find_impulse_legs(itf_clean, trend)

        for leg in impulses:
            zone_low, zone_high = compute_fib_zone(leg)
            touch_index = find_correction_touch(itf_clean, leg, zone_low, zone_high)
            if touch_index is None:
                continue

            trigger_tf = cascade["trigger"]
            trigger_candles = candles_by_tf.get(trigger_tf)
            if not trigger_candles:
                continue

            touch_epoch = itf_clean[touch_index]["epoch"]
            start_index = find_index_after_epoch(trigger_candles, touch_epoch)
            if start_index is None:
                continue

            structure = detect_structure_shift(trigger_candles, start_index, trend)
            if not structure:
                continue

            trade_levels = compute_trade_levels(leg, zone_low, zone_high)

            if trade_levels["risk_reward_1"] is None or trade_levels["risk_reward_1"] < MIN_RISK_REWARD:
                continue

            current_price = trigger_candles[structure["break_index"]]["close"]
            order_type = classify_order_type(
                leg["direction"], trade_levels["entry"], current_price, ORDER_TYPE_TOLERANCE_PCT
            )

            setups.append({
                "symbol": symbol,
                "strategy": "Impulse",
                "direction": leg["direction"],
                "order_type": order_type,
                "htf": htf_tf,
                "zone_tf": itf_tf,
                "trigger_tf": trigger_tf,
                "zone_start_epoch": itf_clean[leg["start_index"]]["epoch"],
                "zone_end_epoch": itf_clean[leg["end_index"]]["epoch"],
                "trigger_epoch": trigger_candles[structure["break_index"]]["epoch"],
                "entry": trade_levels["entry"],
                "stop_loss": trade_levels["stop_loss"],
                "take_profit_1": trade_levels["take_profit_1"],
                "take_profit_2": trade_levels["take_profit_2"],
                "risk_reward_1": trade_levels["risk_reward_1"],
                "risk_reward_2": trade_levels["risk_reward_2"],
                "trend": trend,
                "trend_tf": htf_tf,
                # L'impulsion est filtrée pour n'aller que dans le sens de la
                # tendance HTF (voir find_impulse_legs) : toujours False ici,
                # champ gardé pour un affichage cohérent avec la stratégie CRT.
                "counter_trend": False,
            })

    return setups
