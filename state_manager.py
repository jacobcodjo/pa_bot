import json
import os
import time

from config import STATE_FILE, STATE_MAX_AGE_DAYS
from data_client import is_forex_or_gold


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def setup_key(setup: dict) -> str:
    # Verrou basé sur la zone elle-même (jambe impulsive ou range+sweep selon
    # la stratégie) : une même zone ne peut déclencher qu'une seule alerte
    # par stratégie, peu importe combien de fois le prix la retouche.
    return (
        f"{setup['strategy']}_{setup['symbol']}_{setup['zone_tf']}_"
        f"{setup['zone_start_epoch']}_{setup['zone_end_epoch']}"
    )


def _content_decimals(symbol: str) -> int:
    # Précision alignée sur ce qui est réellement significatif pour l'actif
    # (pas une précision brute à 6 décimales) -- une JPY-pair ou une crypto
    # dont le calcul dérive de quelques millièmes entre deux évaluations du
    # même événement (ex: l'Order Block sélectionné décale d'une bougie)
    # doit quand même être reconnue comme "le même setup", pas comme un
    # nouveau. 2 décimales ~ le pip/tick pratique pour ces actifs ; 4 pour
    # les autres paires forex/or.
    return 2 if "JPY" in symbol or not is_forex_or_gold(symbol) else 4


def content_key(setup: dict) -> str:
    decimals = _content_decimals(setup["symbol"])
    entry = round(setup["entry"], decimals) if setup.get("entry") is not None else None
    stop_loss = round(setup["stop_loss"], decimals)
    take_profit_1 = round(setup["take_profit_1"], decimals)
    return f"content_{setup['strategy']}_{setup['symbol']}_{setup['direction']}_{entry}_{stop_loss}_{take_profit_1}"


def is_new_setup(state: dict, setup: dict) -> bool:
    return setup_key(setup) not in state and content_key(setup) not in state


def mark_setup_sent(state: dict, setup: dict):
    now = time.time()
    state[setup_key(setup)] = now
    state[content_key(setup)] = now


def prune_state(state: dict, max_age_days: float = STATE_MAX_AGE_DAYS) -> int:
    cutoff = time.time() - max_age_days * 86400
    stale_keys = [
        k for k, v in state.items()
        if not isinstance(v, (int, float)) or v < cutoff
    ]
    for k in stale_keys:
        del state[k]
    return len(stale_keys)
