"""
Client REST pour l'API publique Kraken (OHLC) -- pas de clé nécessaire pour
lire des données de marché.

Remplace Binance : l'API publique de Binance (api.binance.com) renvoie une
erreur 451 "Unavailable For Legal Reasons" depuis les adresses IP des
datacenters cloud américains (Azure, AWS) -- exactement l'infrastructure
utilisée par les runners GitHub Actions. Ce n'est pas un problème de quota
ni de code : Binance bloque la connexion à la source, donc aucune retentative
n'y change rien. Kraken n'applique pas ce type de restriction géographique
sur son API publique.
"""

import requests

from config import KRAKEN_BASE_URL, KRAKEN_INTERVAL

# Kraken utilise ses propres tickers (XBT au lieu de BTC, pas de suffixe USDT).
_SYMBOL_MAP = {
    "BTCUSDT": "XBTUSD",
    "ETHUSDT": "ETHUSD",
    "LTCUSDT": "LTCUSD",
    "XRPUSDT": "XRPUSD",
}


def get_candles(symbol: str, tf: str, count: int, timeout: int = 20):
    kraken_pair = _SYMBOL_MAP.get(symbol, symbol)
    params = {"pair": kraken_pair, "interval": KRAKEN_INTERVAL[tf]}

    response = requests.get(f"{KRAKEN_BASE_URL}/OHLC", params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    if data.get("error"):
        raise RuntimeError(f"Kraken ({symbol}/{tf}) : {data['error']}")

    result = data.get("result", {})
    # La clé renvoyée par Kraken pour une paire peut différer du ticker envoyé
    # (ex: XBTUSD -> "XXBTZUSD") -- on prend la seule clé qui n'est pas "last".
    pair_keys = [k for k in result if k != "last"]
    if not pair_keys:
        raise RuntimeError(f"Kraken ({symbol}/{tf}) : réponse inattendue -- {data}")

    rows = result[pair_keys[0]]
    candles = [
        {
            "epoch": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
        }
        for row in rows
    ]
    return candles[-count:]
