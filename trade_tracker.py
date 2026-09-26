import json
import os
import time

from data_client import is_forex_or_gold
from config import STATS_FILE, PENDING_MAX_AGE_DAYS, TRADE_HISTORY_MAX_AGE_DAYS


def load_stats():
    if not os.path.exists(STATS_FILE):
        return {"pending": {}, "history": []}
    with open(STATS_FILE, "r") as f:
        return json.load(f)


def save_stats(stats: dict):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)


def track_key(setup: dict) -> str:
    return (
        f"{setup['strategy']}_{setup['symbol']}_{setup['zone_tf']}_"
        f"{setup['zone_start_epoch']}_{setup['zone_end_epoch']}"
    )


def add_pending(stats: dict, setup: dict):
    key = track_key(setup)
    order_type = setup.get("order_type", "Buy" if setup["direction"] == "bullish" else "Sell")
    stats["pending"][key] = {
        "symbol": setup["symbol"],
        "strategy": setup["strategy"],
        "direction": setup["direction"],
        "order_type": order_type,
        "trigger_tf": setup["trigger_tf"],
        "entry": setup["entry"],
        "stop_loss": setup["stop_loss"],
        "take_profit_1": setup["take_profit_1"],
        "take_profit_2": setup["take_profit_2"],
        "risk_reward_1": setup.get("risk_reward_1"),
        "alert_epoch": setup["trigger_epoch"],
        "filled": order_type in ("Buy", "Sell"),
        "fill_epoch": setup["trigger_epoch"] if order_type in ("Buy", "Sell") else None,
    }


def _check_entry_fill(trade: dict, candles: list):
    entry = trade["entry"]
    if entry is None:
        return None
    for c in candles:
        if c["epoch"] <= trade["alert_epoch"]:
            continue
        if trade["order_type"] == "Buy Limit":
            hit = c["low"] <= entry
        elif trade["order_type"] == "Sell Limit":
            hit = c["high"] >= entry
        elif trade["order_type"] == "Buy Stop":
            hit = c["high"] >= entry
        elif trade["order_type"] == "Sell Stop":
            hit = c["low"] <= entry
        else:
            hit = True
        if hit:
            return c["epoch"]
    return None


def resolve_pending(stats: dict, candles_lookup):
    """candles_lookup : fonction (symbol, trigger_tf) -> liste de bougies ou None."""
    resolved_keys = []

    for key, trade in stats["pending"].items():
        candles = candles_lookup(trade["symbol"], trade["trigger_tf"])
        if not candles:
            continue

        if not trade["filled"]:
            fill_epoch = _check_entry_fill(trade, candles)
            if fill_epoch is None:
                continue
            trade["filled"] = True
            trade["fill_epoch"] = fill_epoch

        result = None
        for c in candles:
            if c["epoch"] <= trade["fill_epoch"]:
                continue

            if trade["direction"] == "bullish":
                hit_tp = c["high"] >= trade["take_profit_1"]
                hit_sl = c["low"] <= trade["stop_loss"]
            else:
                hit_tp = c["low"] <= trade["take_profit_1"]
                hit_sl = c["high"] >= trade["stop_loss"]

            if hit_tp and hit_sl:
                result = "SL"
                break
            if hit_sl:
                result = "SL"
                break
            if hit_tp:
                result = "TP"
                break

        if result:
            stats["history"].append({
                "symbol": trade["symbol"],
                "strategy": trade.get("strategy", "?"),
                "direction": trade["direction"],
                "order_type": trade["order_type"],
                "risk_reward": trade["risk_reward_1"],
                "result": result,
                "resolved_epoch": c["epoch"],
            })
            resolved_keys.append(key)

    for key in resolved_keys:
        del stats["pending"][key]

    return resolved_keys


def expire_stale_pending(stats: dict, max_age_days: float = PENDING_MAX_AGE_DAYS) -> int:
    cutoff = time.time() - max_age_days * 86400
    expired_keys = [
        key for key, trade in stats["pending"].items()
        if not trade.get("filled") and trade["alert_epoch"] < cutoff
    ]
    for key in expired_keys:
        trade = stats["pending"].pop(key)
        stats["history"].append({
            "symbol": trade["symbol"],
            "strategy": trade.get("strategy", "?"),
            "direction": trade["direction"],
            "order_type": trade["order_type"],
            "risk_reward": trade["risk_reward_1"],
            "result": "EXPIRE",
            "resolved_epoch": time.time(),
        })
    return len(expired_keys)


def prune_history(stats: dict, max_age_days: float = TRADE_HISTORY_MAX_AGE_DAYS) -> int:
    cutoff = time.time() - max_age_days * 86400
    before = len(stats["history"])
    stats["history"] = [h for h in stats["history"] if h.get("resolved_epoch", time.time()) >= cutoff]
    return before - len(stats["history"])


def classify_asset(symbol: str) -> str:
    return "Forex/Or" if is_forex_or_gold(symbol) else "Crypto"


def summarize(stats: dict) -> str:
    history = stats.get("history", [])
    if not history:
        return "Aucun trade résolu pour le moment."

    resolved = [h for h in history if h["result"] in ("TP", "SL")]
    expired = [h for h in history if h["result"] == "EXPIRE"]

    def win_rate(trades):
        if not trades:
            return None
        wins = sum(1 for t in trades if t["result"] == "TP")
        return round(100 * wins / len(trades), 1), len(trades)

    overall = win_rate(resolved)
    lines = []
    if overall:
        lines.append(f"Global : {overall[0]}% de réussite sur {overall[1]} trades résolus")

    by_class = {}
    for t in resolved:
        by_class.setdefault(classify_asset(t["symbol"]), []).append(t)
    for class_name in ("Forex/Or", "Crypto"):
        wr = win_rate(by_class.get(class_name, []))
        if wr:
            lines.append(f"{class_name} : {wr[0]}% sur {wr[1]} trades")

    by_strategy = {}
    for t in resolved:
        by_strategy.setdefault(t.get("strategy", "?"), []).append(t)
    for strategy_name, trades in by_strategy.items():
        wr = win_rate(trades)
        if wr:
            lines.append(f"{strategy_name} : {wr[0]}% sur {wr[1]} trades")

    total_with_expired = len(resolved) + len(expired)
    if expired and total_with_expired:
        fill_rate = round(100 * len(resolved) / total_with_expired, 1)
        lines.append(
            f"Taux de remplissage : {fill_rate}% ({len(resolved)} remplis / {len(expired)} expirés)"
        )

    return " | ".join(lines) if lines else "Aucun trade résolu pour le moment."
