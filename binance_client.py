"""
Client REST pour l'API publique Binance (klines) -- pas de clé nécessaire
pour lire des données de marché. Endpoint stable, sans limite gênante à la
fréquence d'appel de ce bot.
"""

import requests

from config import BINANCE_BASE_URL, BINANCE_INTERVAL


def get_candles(symbol: str, tf: str, count: int, timeout: int = 20):
    url = f"{BINANCE_BASE_URL}/klines"
    params = {"symbol": symbol, "interval": BINANCE_INTERVAL[tf], "limit": count}

    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    candles = []
    for k in data:
        candles.append({
            "epoch": int(k[0]) // 1000,  # open_time en ms -> secondes
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
        })
    return candles
