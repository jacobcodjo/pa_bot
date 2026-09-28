"""
Implémentation de la méthodologie Candle Range Trading (CRT), identique aux
règles du bot CRT d'origine :

1. Range de référence = haut/bas de la dernière bougie HTF clôturée (W1/D1/H4).
2. Sweep de liquidité = une bougie (sur le MTF) dépasse ce range puis se
   referme à l'intérieur (piège / stop hunt).
3. Confirmation = cassure de structure (MSS) sur le TF de confirmation, dans
   le sens du retournement, accompagnée d'un Fair Value Gap (FVG) et/ou d'un
   Order Block (OB) sur le MTF.
4. Entrée sur le POI (Order Block > Breaker Block > FVG) détecté au niveau de
   la cassure de structure. Stop au-delà de l'extrême du sweep. Cibles :
   mi-range (TP1) puis bord opposé du range (TP2).

Implémentation simplifiée des concepts ICT / Smart Money Concepts -- outil
d'aide à la décision, à backtester/affiner avant tout usage réel.
"""

from datetime import datetime, timezone

from data_client import is_forex_or_gold
from common import (
    has_weekend_gaps, is_gap_candle, find_swing_highs_lows, detect_trend,
    find_index_after_epoch, detect_structure_shift,
    classify_order_type, GRANULARITY_SECONDS,
)
from config import (
    TREND_SWING_WINDOW, TREND_SWING_COUNT, TREND_SMA_PERIOD,
    TIMEFRAME_CASCADE_CRT, TIMEFRAME_CASCADE_CRT_CRYPTO_EXTRA,
    STRUCTURE_SWING_WINDOW, DEFAULT_STRUCTURE_SWING_WINDOW,
    STRICT_CONFIRMATION_TIMEFRAMES, CRT_STOP_LOSS_BUFFER_PCT, MIN_RISK_REWARD,
    ORDER_TYPE_TOLERANCE_PCT, REQUIRE_FIB_OTE,
    LIQUIDITY_POOL_SWING_WINDOW, LIQUIDITY_POOL_TOLERANCE_PCT, STOP_LOSS_POOL_BUFFER_PCT,
)


def last_closed_candle(candles):
    if len(candles) < 2:
        return None
    return candles[-2]


def get_reference_range(htf_candles):
    if not htf_candles:
        return None
    ref = last_closed_candle(htf_candles)
    if not ref:
        return None
    return {"high": ref["high"], "low": ref["low"], "epoch": ref["epoch"]}


def build_weekly_range(d1_candles):
    """Range W1 (haut/bas de la dernière semaine calendaire complète)."""
    if not d1_candles or len(d1_candles) < 2:
        return None
    closed = d1_candles[:-1]

    weeks = {}
    for c in closed:
        dt = datetime.fromtimestamp(c["epoch"], tz=timezone.utc)
        key = dt.isocalendar()[:2]
        weeks.setdefault(key, []).append(c)

    if len(weeks) < 2:
        return None

    last_complete_key = sorted(weeks.keys())[-2]
    week_candles = weeks[last_complete_key]

    return {
        "high": max(c["high"] for c in week_candles),
        "low": min(c["low"] for c in week_candles),
        "epoch": week_candles[-1]["epoch"],
    }


def group_equal_levels(values, tolerance_pct=LIQUIDITY_POOL_TOLERANCE_PCT):
    if not values:
        return []
    sorted_vals = sorted(values)
    groups = []
    current = [sorted_vals[0]]
    for v in sorted_vals[1:]:
        if abs(v - current[-1]) / current[-1] <= tolerance_pct:
            current.append(v)
        else:
            if len(current) >= 2:
                groups.append(sum(current) / len(current))
            current = [v]
    if len(current) >= 2:
        groups.append(sum(current) / len(current))
    return groups


def find_liquidity_pools(candles, swing_window=LIQUIDITY_POOL_SWING_WINDOW,
                          tolerance_pct=LIQUIDITY_POOL_TOLERANCE_PCT):
    """Détecte les pools de liquidité (Equal Highs / Equal Lows)."""
    highs_idx, lows_idx = find_swing_highs_lows(candles, swing_window, swing_window)
    high_pools = group_equal_levels([candles[i]["high"] for i in highs_idx], tolerance_pct)
    low_pools = group_equal_levels([candles[i]["low"] for i in lows_idx], tolerance_pct)
    return high_pools, low_pools


def is_near_level(price, levels, tolerance_pct=LIQUIDITY_POOL_TOLERANCE_PCT):
    return any(abs(price - lvl) / lvl <= tolerance_pct for lvl in levels)


def detect_liquidity_sweep(ltf_candles, range_high, range_low, ref_epoch,
                            granularity_seconds=None, check_gaps=False):
    events = []
    for i, c in enumerate(ltf_candles):
        if c["epoch"] <= ref_epoch:
            continue
        if check_gaps and is_gap_candle(ltf_candles, i, granularity_seconds):
            continue
        if c["high"] > range_high and c["close"] < range_high:
            events.append({"index": i, "direction": "bearish", "candle": c})
        if c["low"] < range_low and c["close"] > range_low:
            events.append({"index": i, "direction": "bullish", "candle": c})
    return events


def detect_fvg(candles, around_index, direction, window=5):
    """Fair Value Gap : déséquilibre 3 bougies proche de l'index donné."""
    start = max(1, around_index - window)
    end = min(len(candles) - 1, around_index + window)
    for i in range(start, end):
        if i + 1 >= len(candles):
            break
        c1, c3 = candles[i - 1], candles[i + 1]
        if direction == "bullish" and c1["high"] < c3["low"]:
            return {"index": i, "top": c3["low"], "bottom": c1["high"]}
        if direction == "bearish" and c1["low"] > c3["high"]:
            return {"index": i, "top": c1["low"], "bottom": c3["high"]}
    return None


def detect_order_block(candles, around_index, direction, window=5):
    """Dernière bougie opposée au mouvement (order block) avant l'impulsion."""
    start = max(0, around_index - window)
    for i in range(around_index, start - 1, -1):
        c = candles[i]
        is_bearish = c["close"] < c["open"]
        is_bullish = c["close"] > c["open"]
        if direction == "bullish" and is_bearish:
            return {"index": i, "high": c["high"], "low": c["low"]}
        if direction == "bearish" and is_bullish:
            return {"index": i, "high": c["high"], "low": c["low"]}
    return None


def detect_breaker_block(candles, pivot_index, direction, window=5):
    """Breaker Block : la bougie opposée au setup, autour du pivot cassé."""
    start = max(0, pivot_index - window)
    end = min(len(candles) - 1, pivot_index + window)
    for i in range(end, start - 1, -1):
        c = candles[i]
        is_bearish = c["close"] < c["open"]
        is_bullish = c["close"] > c["open"]
        if direction == "bullish" and is_bullish:
            return {"index": i, "high": c["high"], "low": c["low"]}
        if direction == "bearish" and is_bearish:
            return {"index": i, "high": c["high"], "low": c["low"]}
    return None


def compute_trade_levels(direction, range_high, range_low, sweep_candle, entry_poi):
    """
    Entrée : bord de l'entry_poi (LTF) détecté au niveau de la cassure de
    structure -- Order Block/Breaker en priorité, sinon milieu du FVG.
    Stop loss : au-delà de l'extrême de la bougie de sweep (MTF), avec marge.
    Cibles : TP1 = mi-range, TP2 = bord opposé du range.
    """
    if entry_poi is None:
        entry = None
    elif entry_poi["kind"] in ("ob", "breaker"):
        entry = entry_poi["high"] if direction == "bullish" else entry_poi["low"]
    else:  # "fvg"
        entry = (entry_poi["top"] + entry_poi["bottom"]) / 2

    if direction == "bullish":
        stop_loss = sweep_candle["low"] * (1 - CRT_STOP_LOSS_BUFFER_PCT)
    else:
        stop_loss = sweep_candle["high"] * (1 + CRT_STOP_LOSS_BUFFER_PCT)

    take_profit_1 = (range_high + range_low) / 2  # mi-range
    take_profit_2 = range_high if direction == "bullish" else range_low  # bord opposé

    def calc_rr(target):
        if entry is None or entry == stop_loss:
            return None
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


def compute_fib_ote(direction, sweep_candle, structure_candle):
    """Zone Fibonacci OTE (61.8%-79%) du mouvement entre le sweep et la cassure."""
    if direction == "bullish":
        leg_start = sweep_candle["low"]
        leg_end = structure_candle["close"]
        leg_range = leg_end - leg_start
        zone_low = leg_end - 0.79 * leg_range
        zone_high = leg_end - 0.618 * leg_range
    else:
        leg_start = sweep_candle["high"]
        leg_end = structure_candle["close"]
        leg_range = leg_start - leg_end
        zone_low = leg_end + 0.618 * leg_range
        zone_high = leg_end + 0.79 * leg_range
    return zone_low, zone_high


def is_within_fib_ote(entry, zone_low, zone_high):
    if entry is None:
        return False
    return zone_low <= entry <= zone_high


def poi_zone(fvg, ob):
    lows, highs = [], []
    if ob:
        lows.append(ob["low"])
        highs.append(ob["high"])
    if fvg:
        lows.append(fvg["bottom"])
        highs.append(fvg["top"])
    if not lows:
        return None
    return min(lows), max(highs)


def ltf_reacted_from_poi(ltf_candles, start_index, break_index, zone_low, zone_high):
    for c in ltf_candles[start_index:break_index + 1]:
        if c["low"] <= zone_high and c["high"] >= zone_low:
            return True
    return False


def analyze_symbol(symbol, candles_by_tf):
    """
    Cascade à 3 niveaux (top-down) : Référence (HTF, range à sweeper) -> MTF
    (sweep + POI) -> Confirmation (LTF, cassure de structure = déclencheur).
    """
    setups = []
    check_gaps = has_weekend_gaps(symbol)

    trend_source = candles_by_tf.get("D1")
    trend = (
        detect_trend(trend_source, TREND_SWING_WINDOW, TREND_SWING_COUNT, TREND_SMA_PERIOD)
        if trend_source else None
    )

    # Niveau de cascade H1->M15->M5 réservé à la crypto (voir config.py) --
    # non appliqué au forex/or pour ne pas alourdir le quota Twelve Data.
    cascades = dict(TIMEFRAME_CASCADE_CRT)
    if not is_forex_or_gold(symbol):
        cascades.update(TIMEFRAME_CASCADE_CRT_CRYPTO_EXTRA)

    for ref_tf, cascade in cascades.items():
        if ref_tf == "W1":
            ref_range = build_weekly_range(candles_by_tf.get("D1"))
        else:
            ref_range = get_reference_range(candles_by_tf.get(ref_tf))

        if not ref_range:
            continue

        mtf_tf = cascade["mtf"]
        mtf_candles = candles_by_tf.get(mtf_tf)
        if not mtf_candles:
            continue

        sweeps = detect_liquidity_sweep(
            mtf_candles, ref_range["high"], ref_range["low"], ref_range["epoch"],
            granularity_seconds=GRANULARITY_SECONDS.get(mtf_tf),
            check_gaps=check_gaps,
        )

        high_pools, low_pools = find_liquidity_pools(mtf_candles)

        for sweep in sweeps:
            fvg = detect_fvg(mtf_candles, sweep["index"], sweep["direction"])
            ob = detect_order_block(mtf_candles, sweep["index"], sweep["direction"])

            if mtf_tf in STRICT_CONFIRMATION_TIMEFRAMES:
                if not (fvg and ob):
                    continue
            else:
                if not fvg and not ob:
                    continue

            pools_swept = high_pools if sweep["direction"] == "bearish" else low_pools
            liquidity_grabbed = is_near_level(
                sweep["candle"]["high" if sweep["direction"] == "bearish" else "low"], pools_swept
            )

            for confirmation_tf in cascade["confirmation"]:
                ltf_candles = candles_by_tf.get(confirmation_tf)
                if not ltf_candles:
                    continue

                start_index = find_index_after_epoch(ltf_candles, sweep["candle"]["epoch"])
                if start_index is None:
                    continue

                swing_window = STRUCTURE_SWING_WINDOW.get(confirmation_tf, DEFAULT_STRUCTURE_SWING_WINDOW)
                structure = detect_structure_shift(
                    ltf_candles, start_index, sweep["direction"],
                    left=swing_window, right=swing_window
                )
                if not structure:
                    continue

                zone = poi_zone(fvg, ob)
                if not zone or not ltf_reacted_from_poi(ltf_candles, start_index, structure["break_index"], *zone):
                    continue

                structure_candle = ltf_candles[structure["break_index"]]

                ltf_window = max(1, structure["break_index"] - start_index)
                entry_poi = None
                poi_kind = None
                ltf_ob = detect_order_block(ltf_candles, structure["break_index"], sweep["direction"], window=ltf_window)
                if ltf_ob:
                    entry_poi = {"kind": "ob", "high": ltf_ob["high"], "low": ltf_ob["low"]}
                    poi_kind = "OB"
                else:
                    ltf_breaker = detect_breaker_block(ltf_candles, structure["pivot_index"], sweep["direction"])
                    if ltf_breaker:
                        entry_poi = {"kind": "breaker", "high": ltf_breaker["high"], "low": ltf_breaker["low"]}
                        poi_kind = "Breaker"
                    else:
                        ltf_fvg = detect_fvg(ltf_candles, structure["break_index"], sweep["direction"])
                        if ltf_fvg:
                            entry_poi = {"kind": "fvg", "top": ltf_fvg["top"], "bottom": ltf_fvg["bottom"]}
                            poi_kind = "FVG"

                if entry_poi is None:
                    continue  # aucun POI LTF -> pas d'entrée précise -> setup ignoré

                trade_levels = compute_trade_levels(
                    sweep["direction"], ref_range["high"], ref_range["low"], sweep["candle"], entry_poi
                )

                opposite_pools = high_pools if sweep["direction"] == "bullish" else low_pools
                if is_near_level(trade_levels["stop_loss"], opposite_pools):
                    buffer = trade_levels["stop_loss"] * STOP_LOSS_POOL_BUFFER_PCT
                    if sweep["direction"] == "bullish":
                        trade_levels["stop_loss"] -= buffer
                    else:
                        trade_levels["stop_loss"] += buffer
                    if trade_levels["entry"] is not None:
                        risk = abs(trade_levels["entry"] - trade_levels["stop_loss"])
                        if risk > 0:
                            trade_levels["risk_reward_2"] = round(
                                abs(trade_levels["take_profit_2"] - trade_levels["entry"]) / risk, 2
                            )

                if trade_levels["risk_reward_2"] is None or trade_levels["risk_reward_2"] < MIN_RISK_REWARD:
                    continue

                fib_zone_low, fib_zone_high = compute_fib_ote(sweep["direction"], sweep["candle"], structure_candle)
                fib_ote_confirmed = is_within_fib_ote(trade_levels["entry"], fib_zone_low, fib_zone_high)

                if REQUIRE_FIB_OTE and not fib_ote_confirmed:
                    continue

                counter_trend = (
                    (trend == "bullish" and sweep["direction"] == "bearish")
                    or (trend == "bearish" and sweep["direction"] == "bullish")
                )

                current_price = ltf_candles[-1]["close"]
                order_type = classify_order_type(
                    sweep["direction"], trade_levels["entry"], current_price, ORDER_TYPE_TOLERANCE_PCT
                )

                setups.append({
                    "symbol": symbol,
                    "strategy": "CRT",
                    "direction": sweep["direction"],
                    "order_type": order_type,
                    "htf": ref_tf,
                    "zone_tf": mtf_tf,
                    "trigger_tf": confirmation_tf,
                    "zone_start_epoch": ref_range["epoch"],
                    "zone_end_epoch": sweep["candle"]["epoch"],
                    "trigger_epoch": structure_candle["epoch"],
                    "entry": trade_levels["entry"],
                    "stop_loss": trade_levels["stop_loss"],
                    "take_profit_1": trade_levels["take_profit_1"],
                    "take_profit_2": trade_levels["take_profit_2"],
                    "risk_reward_1": trade_levels["risk_reward_1"],
                    "risk_reward_2": trade_levels["risk_reward_2"],
                    "trend": trend,
                    "trend_tf": "D1",
                    "counter_trend": counter_trend,
                    "liquidity_grabbed": liquidity_grabbed,
                    "poi_kind": poi_kind,
                    "fib_ote_confirmed": fib_ote_confirmed,
                })

    return setups
