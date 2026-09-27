import os

# ============================================================================
# Sources de données
# ----------------------------------------------------------------------------
# Forex + Or  -> Twelve Data (API de données de marché pures, pas un broker :
#                inscription par email, sans compte de trading ni KYC, sans
#                restriction de pays). Palier gratuit : 800 requêtes/jour,
#                8/minute -- voir data_client.py pour le cache qui respecte
#                cette limite.
# Crypto      -> Kraken REST API publique (endpoint OHLC), sans clé.
# ============================================================================

TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")
TWELVEDATA_BASE_URL = "https://api.twelvedata.com"

KRAKEN_BASE_URL = "https://api.kraken.com/0/public"

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("PA_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("PA_TELEGRAM_CHAT_ID")

# ============================================================================
# Symboles
# ----------------------------------------------------------------------------
# Convention : un symbole contenant "_" est un instrument OANDA (forex/or),
# un symbole sans "_" est une paire Kraken (crypto). C'est ce qui permet à
# data_client.py de savoir vers quelle API router chaque requête.
# ============================================================================

FOREX_MAJORS = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"]
FOREX_MINORS = [
    "EUR_GBP", "EUR_JPY", "EUR_CHF", "EUR_AUD", "EUR_CAD", "EUR_NZD",
    "GBP_JPY", "GBP_CHF", "GBP_AUD", "GBP_CAD", "GBP_NZD",
    "AUD_CAD", "AUD_CHF", "CAD_CHF", "CAD_JPY", "CHF_JPY", "NZD_CAD", "NZD_CHF",
]
GOLD = ["XAU_USD"]
CRYPTOS = ["BTCUSDT", "ETHUSDT", "LTCUSDT", "XRPUSDT"]

ALL_SYMBOLS = FOREX_MAJORS + FOREX_MINORS + GOLD + CRYPTOS

# ============================================================================
# Killzones -- chaque fenêtre est définie en HEURE LOCALE de sa propre place
# financière (fuseau IANA), pas en UTC+1 fixe. Le calcul (sessions.py) convertit
# l'heure UTC actuelle vers le fuseau local de chaque session via `zoneinfo`,
# qui applique automatiquement le changement d'heure (DST) de cette région --
# aucun ajustement manuel à faire quand l'Europe ou les Etats-Unis changent
# d'heure : la même fenêtre "locale" reste valable toute l'année.
#
# Chaque killzone n'active que les symboles qui la concernent le plus, selon
# les places financières ouvertes à ce moment-là.
# ============================================================================

SESSION_WINDOWS = {
    # Session asiatique (Tokyo) : les paires JPY, AUD, NZD sont les plus actives.
    # Le Japon n'observe pas le changement d'heure -> fenêtre stable toute l'année.
    "asian": {"tz": "Asia/Tokyo", "start": (9, 0), "end": (13, 0)},
    # Session de Londres : EUR, GBP, CHF, l'or, et une partie du volume crypto.
    "london": {"tz": "Europe/London", "start": (8, 0), "end": (11, 0)},
    # Session de New York (chevauchement Londres/NY inclus) : paires USD, or, crypto.
    "new_york": {"tz": "America/New_York", "start": (8, 0), "end": (11, 0)},
}

# Fuseau utilisé uniquement pour l'affichage dans les messages Telegram
# (WAT, UTC+1 fixe toute l'année -- pas de DST). Attention : Abidjan est en
# réalité UTC+0 (GMT) ; Lagos (comme la plupart de l'Afrique de l'Ouest
# anglophone/Niger/Tchad) est bien UTC+1 -- à changer ici si besoin d'un autre
# pays UTC+1 (ex: "Africa/Niamey", "Africa/Porto-Novo").
DISPLAY_TZ = "Africa/Lagos"

SESSION_SYMBOLS = {
    "asian": [
        "USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY", "CHF_JPY", "CAD_JPY",
        "AUD_USD", "NZD_CAD", "NZD_CHF", "AUD_CAD", "AUD_CHF",
    ],
    "london": [
        "EUR_USD", "GBP_USD", "EUR_GBP", "EUR_CHF", "GBP_CHF",
        "EUR_AUD", "EUR_CAD", "EUR_NZD", "GBP_AUD", "GBP_CAD", "GBP_NZD",
        "XAU_USD",
    ],
    "new_york": [
        "EUR_USD", "GBP_USD", "USD_CHF", "USD_CAD", "USD_JPY", "CAD_CHF",
        "XAU_USD",
    ],
}

# La crypto trade 24/7 -- contrairement au forex/or, elle n'est pas limitée
# aux killzones et est donc scannée à chaque passage, quelle que soit l'heure.
ALWAYS_ON_SYMBOLS = CRYPTOS

# --- Granularités : mêmes clés internes, converties par chaque client API ---
GRANULARITY = {"D1": "D1", "H4": "H4", "H1": "H1", "M15": "M15"}
TWELVEDATA_INTERVAL = {"D1": "1day", "H4": "4h", "H1": "1h", "M15": "15min"}
KRAKEN_INTERVAL = {"D1": 1440, "H4": 240, "H1": 60, "M15": 15}  # en minutes

# --- Cache de bougies (candle_cache.py) : évite de re-télécharger une bougie
# tant que sa période n'est pas révolue -- indispensable pour rester sous les
# 800 requêtes/jour du palier gratuit Twelve Data. Clé = timeframe interne,
# valeur = durée de validité du cache en secondes (= durée d'une bougie).
CACHE_TTL_SECONDS = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900}
CANDLE_CACHE_FILE = "candle_cache.json"

# --- Outlook hebdomadaire (week-end) : biais directionnel D1/W1 par actif,
# envoyé une seule fois par week-end (marqueur pour éviter les doublons). ---
WEEKLY_OUTLOOK_FILE = "weekly_outlook_state.json"

# --- Cascade à 3 niveaux, stratégie Impulse (tendance -> zone Fibo -> trigger) ---
TIMEFRAME_CASCADE_IMPULSE = {
    "W1": {"itf": "D1", "trigger": "H4"},
    "D1": {"itf": "H4", "trigger": "H1"},
    "H4": {"itf": "H1", "trigger": "M15"},
}

# --- Cascade à 3 niveaux, stratégie CRT (range -> sweep+POI -> confirmation) ---
TIMEFRAME_CASCADE_CRT = {
    "W1": {"mtf": "D1", "confirmation": ["H4"]},
    "D1": {"mtf": "H4", "confirmation": ["H1"]},
    "H4": {"mtf": "H1", "confirmation": ["M15"]},
}

CANDLE_COUNT = 500
STATE_FILE = "state.json"
STATS_FILE = "trade_stats.json"

# --- Détection de tendance : partagée par les deux stratégies ---
TREND_SWING_WINDOW = 4
TREND_SWING_COUNT = 3
TREND_SMA_PERIOD = 50

# --- Règles d'entrée/sortie communes ---
MIN_RISK_REWARD = 3.0
ORDER_TYPE_TOLERANCE_PCT = 0.0005
# Le gap de weekend ne concerne que le forex/or (OANDA), pas la crypto (24/7).
WEEKEND_GAP_MULTIPLIER = 1.5

# --- Stratégie Impulse : jambe impulsive + zone Fibonacci ---
IMPULSE_SWING_WINDOW = 3
IMPULSE_LOOKBACK_LEGS = 5
IMPULSE_SIZE_MULTIPLIER = 1.5
FIB_RETRACEMENT_LOW = 0.618
FIB_RETRACEMENT_HIGH = 0.79
FIB_EXTENSION_TP1 = 1.272
FIB_EXTENSION_TP2 = 1.618
IMPULSE_STOP_LOSS_BUFFER_PCT = 0.001

# --- Stratégie CRT : sweep de liquidité + FVG/Order Block ---
# Fenêtre utilisée pour repérer les pivots lors de la cassure de structure,
# par timeframe de confirmation. Non listé -> DEFAULT_STRUCTURE_SWING_WINDOW.
STRUCTURE_SWING_WINDOW = {"M15": 1}
DEFAULT_STRUCTURE_SWING_WINDOW = 2
# Timeframes où la confirmation est renforcée : FVG ET Order Block exigés ensemble.
STRICT_CONFIRMATION_TIMEFRAMES = []
CRT_STOP_LOSS_BUFFER_PCT = 0.0005
# Confirmation Fibonacci OTE : True -> un setup dont l'entrée est hors zone
# 61.8%-79% est ignoré. False -> juste indiqué dans le message (informatif).
REQUIRE_FIB_OTE = False
# Pools de liquidité (Equal Highs / Equal Lows).
LIQUIDITY_POOL_SWING_WINDOW = 3
LIQUIDITY_POOL_TOLERANCE_PCT = 0.001
STOP_LOSS_POOL_BUFFER_PCT = 0.001

# --- Nettoyage automatique ---
STATE_MAX_AGE_DAYS = 30
PENDING_MAX_AGE_DAYS = 7
TRADE_HISTORY_MAX_AGE_DAYS = 180
