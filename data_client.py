"""
Couche unique au-dessus de twelvedata_client et kraken_client. Un symbole
contenant "_" (ex: EUR_USD, XAU_USD) est routé vers Twelve Data ; un symbole
sans "_" (ex: BTCUSDT) est routé vers Kraken.

Inclut un cache disque (candle_cache.json) : une bougie n'est re-téléchargée
que si sa période est révolue (ex: une H4 n'est refetchée qu'après 4h). Ceci
est indispensable pour rester sous le palier gratuit Twelve Data (800
requêtes/jour, 8/minute) -- sans ce cache, scanner ~4 timeframes x plusieurs
dizaines de symboles à chaque scan dépasserait largement ce quota.
"""

import json
import os
import time

import twelvedata_client
import kraken_client
from config import CACHE_TTL_SECONDS, CANDLE_CACHE_FILE


def is_forex_or_gold(symbol: str) -> bool:
    return "_" in symbol


def load_cache():
    if not os.path.exists(CANDLE_CACHE_FILE):
        return {}
    try:
        with open(CANDLE_CACHE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: dict):
    with open(CANDLE_CACHE_FILE, "w") as f:
        json.dump(cache, f)


def _cache_key(symbol: str, tf: str) -> str:
    return f"{symbol}|{tf}"


def _is_fresh(entry: dict, tf: str, now: float) -> bool:
    return bool(entry) and (now - entry.get("fetched_at", 0)) < CACHE_TTL_SECONDS[tf]


def _fetch_one(symbol: str, tf: str, count: int, max_retries: int = 3, retry_delay: int = 5):
    client = twelvedata_client if is_forex_or_gold(symbol) else kraken_client
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return client.get_candles(symbol, tf, count)
        except Exception as e:
            last_error = e
            print(f"[{symbol}/{tf}] Erreur (tentative {attempt}/{max_retries}) : {e}")
            if attempt < max_retries:
                # Un 429 (quota dépassé) a besoin de bien plus qu'un court
                # délai fixe pour se résorber -- le throttle interne de
                # twelvedata_client devrait déjà l'empêcher, mais on se
                # protège quand même ici en cas de dérive.
                wait = 60 if "429" in str(e) else retry_delay
                time.sleep(wait)
    return last_error


def get_many_candles(specs, pause_seconds: float = 0.2):
    """
    specs : liste de tuples (symbol, tf, count)
    Retourne : dict { (symbol, tf): [candles] ou Exception en cas d'erreur }
    Sert le cache quand il est encore valide pour ce timeframe, sinon
    télécharge et met à jour le cache.
    """
    cache = load_cache()
    now = time.time()
    results = {}
    cache_dirty = False

    for symbol, tf, count in specs:
        key = _cache_key(symbol, tf)
        entry = cache.get(key)

        if _is_fresh(entry, tf, now):
            results[(symbol, tf)] = entry["candles"]
            continue

        result = _fetch_one(symbol, tf, count)
        if isinstance(result, Exception):
            # Echec réseau : on retombe sur le cache existant, même expiré,
            # plutôt que de ne rien avoir du tout pour ce passage.
            if entry:
                print(f"[{symbol}/{tf}] Utilisation du cache expiré (échec réseau).")
                results[(symbol, tf)] = entry["candles"]
            else:
                results[(symbol, tf)] = result
            continue

        results[(symbol, tf)] = result
        cache[key] = {"fetched_at": now, "candles": result}
        cache_dirty = True
        # Twelve Data se régule déjà lui-même (8s entre appels) ; pour Kraken,
        # ~1s entre appels est le délai recommandé pour l'endpoint OHLC public.
        time.sleep(pause_seconds if is_forex_or_gold(symbol) else max(pause_seconds, 1.0))

    if cache_dirty:
        save_cache(cache)

    return results


def get_many_cached(specs):
    """
    Comme get_many_candles, mais ne fait JAMAIS d'appel réseau : renvoie
    uniquement ce qui est déjà présent en cache, même expiré (ou None si le
    symbole/timeframe n'a encore jamais été récupéré). Sert à analyser des
    symboles en dehors de leur killzone sans consommer le moindre quota API
    -- au prix de données potentiellement plus anciennes que d'habitude.

    specs : liste de tuples (symbol, tf, count) -- count est ignoré ici.
    """
    cache = load_cache()
    results = {}
    for symbol, tf, _count in specs:
        entry = cache.get(_cache_key(symbol, tf))
        results[(symbol, tf)] = entry["candles"] if entry else None
    return results
