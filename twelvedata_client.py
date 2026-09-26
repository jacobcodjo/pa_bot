"""
Client REST pour l'API Twelve Data (forex + or). Contrairement à un broker,
Twelve Data n'est qu'un fournisseur de données de marché : inscription par
simple email, sans compte de trading ni vérification d'identité, et sans
restriction de pays -- utilisable depuis n'importe où, y compris les pays
non couverts par OANDA.

Palier gratuit : 800 requêtes/jour, 8/minute -- voir data_client.py pour le
cache qui limite le nombre d'appels réels.
"""

from datetime import datetime, timezone

import requests

from config import TWELVEDATA_API_KEY, TWELVEDATA_BASE_URL, TWELVEDATA_INTERVAL


def _to_api_symbol(symbol: str) -> str:
    return symbol.replace("_", "/")  # EUR_USD -> EUR/USD


def _parse_datetime(dt_str: str) -> int:
    # Avec timezone=UTC, Twelve Data renvoie soit "YYYY-MM-DD HH:MM:SS" soit,
    # pour les bougies journalières, parfois juste "YYYY-MM-DD".
    fmt = "%Y-%m-%d %H:%M:%S" if " " in dt_str else "%Y-%m-%d"
    return int(datetime.strptime(dt_str, fmt).replace(tzinfo=timezone.utc).timestamp())


def get_candles(symbol: str, tf: str, count: int, timeout: int = 20):
    if not TWELVEDATA_API_KEY:
        raise RuntimeError("TWELVEDATA_API_KEY manquant (variable d'environnement).")

    params = {
        "symbol": _to_api_symbol(symbol),
        "interval": TWELVEDATA_INTERVAL[tf],
        "outputsize": count,
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    response = requests.get(f"{TWELVEDATA_BASE_URL}/time_series", params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(f"Twelve Data ({symbol}/{tf}) : {data.get('message', data)}")

    candles = [
        {
            "epoch": _parse_datetime(v["datetime"]),
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
        }
        for v in data["values"]
    ]
    candles.sort(key=lambda c: c["epoch"])  # Twelve Data renvoie le plus récent en premier
    return candles
