"""Quatre stratégies pour l'or (XAUUSD), les paires forex majeures et BTC/ETH. Toutes les règles sont décrites dans STRATEGIES.md.

Module autonome : aucune entrée/sortie réseau. Chaque stratégie reçoit provide(tf) -> bougies du
timeframe (la dernière est en formation) et renvoie un signal (dict) ou None.

  S1  Trend Pullback              H4 (tendance) > H1 (repli 50-79 %) > M15 (cassure de structure)
  S2  Asian Breakout                  M15, fenêtre 02h00-07h00 New York
  S3  Sweep + CHoCH                   M15 (sweep) > M5 (cassure de structure), killzones
  S4  Mean Reversion                  H1 (régime) > M15 (rejet des bandes de Bollinger)

Les heures sont en heure de New York ; D1 et H4 ouvrent à 17h New York.
"""
import statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
GRAN = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}

# --- Paramètres communs ---
ATR_N = 14
DISP_MULT = 1.2           # corps de la bougie de cassure >= 1,2 x corps moyen (20 dernières bougies)
FRESH = 2                 # la bougie de déclenchement doit être l'une des 2 dernières clôturées
BUF_ATR = 0.15            # marge du stop au-delà de l'extrême : 0,15 x ATR du timeframe d'entrée
MIN_RISK_ATR = 0.5        # risque minimal : 0,5 x ATR du timeframe d'entrée (sinon le stop est éloigné)
CHASE_ATR = 0.75          # le prix ne doit pas avoir fui de plus de 0,75 x ATR de la clôture de déclenchement
TP_MULTS = (1.0, 2.0, 3.0)  # TP1, TP2, TP3 en multiples du risque (S1, S2, S3)

KILLZONES_NY = [("Asie", 20, 24), ("Asie", 0, 2), ("Londres", 2, 5), ("New York", 7, 10), ("London Close", 10, 12)]

# --- S1 Trend Pullback ---
S1_EMA_FAST, S1_EMA_SLOW = 50, 200     # EMA du H4
S1_ADX_MIN = 18                        # ADX(14) du H4 minimal : écarte les marchés sans direction
S1_SWING_H4 = 2                        # fractale H4 : 2 bougies de chaque côté
S1_SWING_H1 = 3                        # fractale H1 : 3 bougies de chaque côté
S1_LEG_BARS = 24                       # la jambe démarre au plus bas (haut) des 24 H1 qui précèdent le sommet (creux)
S1_LEG_MIN_ATR, S1_LEG_MAX_ATR = 1.5, 5.0   # taille de la jambe impulsive H1, en ATR H1
S1_FIB_MIN, S1_FIB_MAX = 0.50, 0.85    # le repli doit atteindre 50 % et ne pas dépasser 85 % de la jambe
S1_ZONE = (0.50, 0.79)                 # zone de repli affichée (50 % - 79 %)
S1_RISK_CAP_H1 = 1.5                   # risque maximal : 1,5 x ATR H1

# --- S2 Cassure de la plage asiatique ---
S2_RANGE_MIN_ATR, S2_RANGE_MAX_ATR = 0.4, 2.0   # taille de la plage asiatique, en ATR H1
S2_WINDOW = (2, 7)                     # cassure valable de 02h00 à 07h00 New York
S2_CLOSE_STRENGTH = 0.7                # la bougie doit clôturer dans les 30 % extrêmes de son amplitude
S2_RISK_CAP_H1 = 2.0
S2_H4_FILTER = True                    # sens aligné avec l'EMA 50 du H4

# --- S3 Prise de liquidité + CHoCH ---
S3_EQ_TOL_ATR = 0.15                   # equal highs/lows : deux swings H1 à moins de 0,15 ATR H1
S3_SWEEP_BARS = 8                      # le sweep M15 date de 8 bougies M15 au plus (2 h)
S3_PIVOT_BARS = 36                     # pivot M5 à moins de 36 bougies M5 (3 h) du sommet/creux du sweep
S3_SESSIONS = ("Londres", "New York", "London Close")
S3_RISK_CAP_H1 = 1.5

# --- S4 Mean Reversion ---
S4_ADX_MAX = 20                        # ADX(14) du H1 sous 20
S4_FLAT_ATR = 0.6                      # EMA 50 du H1 : variation sur 12 bougies sous 0,6 x ATR H1
S4_BB_N, S4_BB_K = 20, 2.0
S4_RSI_N, S4_RSI_LOW, S4_RSI_HIGH = 14, 30, 70
S4_BUF_ATR = 0.3
S4_HOURS = [(20, 26), (11, 15)]        # 20h-02h et 11h-15h New York (26 = 02h du lendemain)
S4_MIN_RR1, S4_MIN_RR2 = 0.8, 1.5      # ratios minimaux vers TP1 et TP2
S4_RISK_CAP_H1 = 1.0


# ==========================================================================
# Indicateurs
# ==========================================================================
def body(c):
    return abs(c["close"] - c["open"])


def body_avg(cs, n=20):
    cs = cs[-n:]
    return sum(body(c) for c in cs) / len(cs) if cs else 0.0


def atr(cs, n=ATR_N):
    cs = cs[-(n + 1):]
    trs = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
           for p, c in zip(cs, cs[1:])]
    return sum(trs) / len(trs) if trs else 0.0


def ema(vals, n):
    """EMA (graine = moyenne simple des n premières valeurs). None avant la n-ième valeur."""
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi_series(closes, n=14):
    """RSI de Wilder pour chaque bougie (None au début)."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, n + 1)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, n + 1)]
    ag, al = sum(gains) / n, sum(losses) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def adx(cs, n=14):
    """ADX de Wilder (dernière valeur), ou None si l'historique est insuffisant (< 2n + 1 bougies)."""
    if len(cs) < 2 * n + 1:
        return None
    tr, pdm, mdm = [], [], []
    for p, c in zip(cs, cs[1:]):
        up, dn = c["high"] - p["high"], p["low"] - c["low"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    s_tr, s_p, s_m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs = []
    for i in range(n, len(tr) + 1):
        if i > n:
            s_tr = s_tr - s_tr / n + tr[i - 1]
            s_p = s_p - s_p / n + pdm[i - 1]
            s_m = s_m - s_m / n + mdm[i - 1]
        if s_tr <= 0:
            dxs.append(0.0)
            continue
        pdi, mdi = 100 * s_p / s_tr, 100 * s_m / s_tr
        dxs.append(0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return a


def bollinger(closes, i, n=S4_BB_N, k=S4_BB_K):
    """(bande basse, moyenne, bande haute) à la bougie d'index i, ou None."""
    if i + 1 < n:
        return None
    w = closes[i + 1 - n:i + 1]
    m, sd = sum(w) / n, statistics.pstdev(w)
    return m - k * sd, m, m + k * sd


def swings(cs, kind, n):
    """Index des swings confirmés (fractales) : plus haut/bas strict sur n bougies de chaque côté.
    `cs` ne doit contenir que des bougies clôturées."""
    out = []
    for i in range(n, len(cs) - n):
        v = cs[i][kind]
        around = [cs[j][kind] for j in range(i - n, i + n + 1) if j != i]
        if (kind == "high" and v > max(around)) or (kind == "low" and v < min(around)):
            out.append(i)
    return out


# ==========================================================================
# Temps et sessions (heure de New York)
# ==========================================================================
def session_name(epoch):
    h = datetime.fromtimestamp(epoch, NY).hour
    for name, a, b in KILLZONES_NY:
        if a <= h < b:
            return name
    return "Hors session"


def asia_window(now):
    """(début, fin) en epoch de la plage asiatique en cours ou la plus récente : 20h00 -> 02h00 New York."""
    dt = datetime.fromtimestamp(now, NY)
    start = (dt - timedelta(days=1) if dt.hour < 20 else dt).replace(hour=20, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=1)).replace(hour=2)
    return int(start.timestamp()), int(end.timestamp())


def in_hours(epoch, windows):
    h = datetime.fromtimestamp(epoch, NY).hour
    return any(a <= h < b or a <= h + 24 < b for a, b in windows)


# ==========================================================================
# Niveaux d'entrée, stop et objectifs
# ==========================================================================
def build_levels(d, entry, sl, a_ltf, a_h1, cap_h1, tps=None):
    """Applique le risque minimal (stop élargi si trop proche) et le plafond (signal rejeté si le
    risque dépasse cap_h1 x ATR H1). tps : liste de 3 objectifs ; sinon 1R / 2R / 3R."""
    risk = abs(entry - sl)
    floor = MIN_RISK_ATR * a_ltf
    if risk < floor:
        sl = entry - floor if d == "BUY" else entry + floor
        risk = floor
    if risk <= 0 or (a_h1 > 0 and risk > cap_h1 * a_h1):
        return None
    sgn = 1 if d == "BUY" else -1
    return {"sl": sl, "risk": risk, "tp": tps or [entry + sgn * m * risk for m in TP_MULTS]}


def is_late(d, price, trig_close, a_ltf):
    """Vrai si le prix a déjà trop fui dans le sens du trade depuis la clôture de déclenchement."""
    move = (price - trig_close) if d == "BUY" else (trig_close - price)
    return a_ltf > 0 and move > CHASE_ATR * a_ltf


def _signal(strat, label, d, price, lv, ltf, ltf_n, key, details, **kw):
    sig = {"strat": strat, "label": label, "dir": d, "otype": d, "entry": price, "sl": lv["sl"],
           "tp1": lv["tp"][0], "tp2": lv["tp"][1], "tp3": lv["tp"][2], "ltf": ltf_n, "gran": GRAN[ltf_n],
           "key": f"{strat}|{d}|{key}", "details": details, "lines": [], "zones": [], "inval": None,
           "extra": {}, "session": session_name(ltf[-1]["epoch"])}
    sig.update(kw)
    return sig


def h4_ema_bias(h4):
    """+1 si le dernier H4 clôturé est au-dessus de l'EMA 50, -1 sinon, 0 si inconnu."""
    closes = [c["close"] for c in h4[:-1]]
    e = ema(closes, S1_EMA_FAST)[-1] if closes else None
    if e is None:
        return 0
    return 1 if closes[-1] > e else -1


# ==========================================================================
# S1  Trend Pullback
# ==========================================================================
def s1_trend(h4c):
    """'BUY' / 'SELL' / None. Tendance haussière : EMA50 > EMA200, clôture > EMA200, ADX >= 18 et dernier
    creux H4 plus haut que le précédent. Baissière : l'inverse (sommet plus bas)."""
    if len(h4c) < S1_EMA_SLOW + 10:
        return None
    closes = [c["close"] for c in h4c]
    fast, slow = ema(closes, S1_EMA_FAST)[-1], ema(closes, S1_EMA_SLOW)[-1]
    a = adx(h4c)
    if fast is None or slow is None or a is None or a < S1_ADX_MIN:
        return None
    if fast > slow and closes[-1] > slow:
        lows = swings(h4c, "low", S1_SWING_H4)
        if len(lows) >= 2 and h4c[lows[-1]]["low"] > h4c[lows[-2]]["low"]:
            return "BUY"
    if fast < slow and closes[-1] < slow:
        highs = swings(h4c, "high", S1_SWING_H4)
        if len(highs) >= 2 and h4c[highs[-1]]["high"] < h4c[highs[-2]]["high"]:
            return "SELL"
    return None


def scan_s1(provide, now, funnel, dec=2):
    h4, h1, m15 = provide("H4"), provide("H1"), provide("M15")
    if not (h4 and h1 and m15):
        return None
    h4c, h1c, m15c = h4[:-1], h1[:-1], m15[:-1]
    d = s1_trend(h4c)
    if d is None or len(h1c) < 60 or len(m15c) < 40:
        return None
    funnel["S1_tendance"] += 1
    buy = d == "BUY"
    a_h1, a_m15 = atr(h1c), atr(m15c)
    if a_h1 <= 0 or a_m15 <= 0:
        return None
    # --- jambe impulsive H1 : dernier swing (sommet si achat) et swing opposé qui le précède ---
    hk, lk = ("high", "low") if buy else ("low", "high")
    tops = swings(h1c, hk, S1_SWING_H1)
    if not tops:
        return None
    ti = tops[-1]
    if len(h1c) - 1 - ti > 48:
        return None                                  # sommet trop ancien
    top = h1c[ti][hk]
    if any((c["high"] > top) if buy else (c["low"] < top) for c in h1c[ti + 1:]):
        return None                                  # nouvel extrême en cours : on attend sa confirmation
    window = h1c[max(0, ti - S1_LEG_BARS):ti + 1]          # la jambe démarre au plus bas (haut) des 24 H1 précédents
    base = min(c["low"] for c in window) if buy else max(c["high"] for c in window)
    leg = abs(top - base)
    if not (S1_LEG_MIN_ATR * a_h1 <= leg <= S1_LEG_MAX_ATR * a_h1):
        return None
    # --- repli : extrême atteint depuis le sommet (M15 clôturées après la clôture de la bougie H1 du sommet) ---
    after = [(i, c) for i, c in enumerate(m15c) if c["epoch"] >= h1c[ti]["epoch"] + GRAN["H1"]]
    if len(after) < 4:
        return None
    pb_i, pb = (min(after, key=lambda x: (x[1]["low"], -x[0])) if buy
                else max(after, key=lambda x: (x[1]["high"], x[0])))
    pb_px = pb["low"] if buy else pb["high"]
    retr = abs(top - pb_px) / leg
    if not (S1_FIB_MIN <= retr <= S1_FIB_MAX):
        return None
    funnel["S1_repli"] += 1
    # --- déclencheur M15 : cassure du dernier swing contraire formé pendant le repli ---
    sk = "high" if buy else "low"
    piv = [i for i in swings(m15c, sk, 2) if i < pb_i and m15c[i]["epoch"] > h1c[ti]["epoch"]]
    if not piv:
        return None
    level = m15c[piv[-1]][sk]
    avg = body_avg(m15c[-21:-1])
    trig = None
    for i in range(pb_i + 1, len(m15c)):
        c = m15c[i]
        if (c["close"] > level) if buy else (c["close"] < level):
            trig = i
            break
    if trig is None or trig < len(m15c) - FRESH:
        return None
    tc = m15c[trig]
    if body(tc) < DISP_MULT * avg:
        return None
    price = m15[-1]["close"]
    if is_late(d, price, tc["close"], a_m15):
        funnel["stale"] += 1
        return None
    sl = pb_px - BUF_ATR * a_m15 if buy else pb_px + BUF_ATR * a_m15
    lv = build_levels(d, price, sl, a_m15, a_h1, S1_RISK_CAP_H1)
    if lv is None:
        return None
    funnel["S1_signal"] += 1
    zone = sorted([top - S1_ZONE[0] * leg, top - S1_ZONE[1] * leg]) if buy else \
        sorted([top + S1_ZONE[0] * leg, top + S1_ZONE[1] * leg])
    return _signal(
        "S1", "Trend Pullback", d, price, lv, m15, "M15", f"{h1c[ti]['epoch']}|{pb['epoch']}|{tc['epoch']}",
        [f"✅ Tendance H4 {'haussière' if buy else 'baissière'} (EMA 50/200, ADX {adx(h4c):.0f})",
         f"✅ Repli H1 à {retr * 100:.0f} % de la jambe ({top:.{dec}f} → {base:.{dec}f})",
         f"✅ Cassure de structure M15 à {level:.{dec}f}"],
        lines=[{"y": level, "label": "Cassure", "kind": "range"}, {"y": pb_px, "label": "Repli", "kind": "sweep"}],
        zones=[{"lo": zone[0], "hi": zone[1], "label": "Fib 50-79 %", "kind": "fib"}],
        inval=pb_px, extra={"retr": round(retr, 2)})


# ==========================================================================
# S2  Asian Breakout (cassure de la plage asiatique)
# ==========================================================================
def scan_s2(provide, now, funnel, dec=2):
    h1, m15, h4 = provide("H1"), provide("M15"), provide("H4")
    if not (h1 and m15 and h4):
        return None
    a0, a1 = asia_window(now)
    hour = datetime.fromtimestamp(now, NY).hour
    if not (S2_WINDOW[0] <= hour < S2_WINDOW[1]) or now < a1:
        return None
    m15c, h1c = m15[:-1], h1[:-1]
    asia = [c for c in m15c if a0 <= c["epoch"] < a1]
    if len(asia) < 20:
        return None                                   # plage incomplète (données manquantes)
    hi, lo = max(c["high"] for c in asia), min(c["low"] for c in asia)
    a_h1, a_m15 = atr(h1c), atr(m15c)
    if a_h1 <= 0 or a_m15 <= 0 or not (S2_RANGE_MIN_ATR * a_h1 <= hi - lo <= S2_RANGE_MAX_ATR * a_h1):
        return None
    funnel["S2_range"] += 1
    post = [(i, c) for i, c in enumerate(m15c) if c["epoch"] >= a1]
    first = next(((i, c) for i, c in post if c["close"] > hi or c["close"] < lo), None)
    if first is None or first[0] < len(m15c) - FRESH:
        return None                                   # seule la première cassure compte, et elle doit être récente
    i, tc = first
    d = "BUY" if tc["close"] > hi else "SELL"
    rng = tc["high"] - tc["low"]
    strong = rng > 0 and ((tc["close"] - tc["low"]) / rng >= S2_CLOSE_STRENGTH if d == "BUY"
                          else (tc["high"] - tc["close"]) / rng >= S2_CLOSE_STRENGTH)
    if not strong or body(tc) < DISP_MULT * body_avg(m15c[-21:-1]):
        return None
    bias = h4_ema_bias(h4)
    if S2_H4_FILTER and bias != (1 if d == "BUY" else -1):
        return None
    price = m15[-1]["close"]
    if is_late(d, price, tc["close"], a_m15):
        funnel["stale"] += 1
        return None
    sl = (lo - BUF_ATR * a_m15) if d == "BUY" else (hi + BUF_ATR * a_m15)
    lv = build_levels(d, price, sl, a_m15, a_h1, S2_RISK_CAP_H1)
    if lv is None:
        return None
    funnel["S2_signal"] += 1
    return _signal(
        "S2", "Asian Breakout", d, price, lv, m15, "M15", f"{a0}|{tc['epoch']}",
        [f"✅ Plage asiatique {lo:.{dec}f} - {hi:.{dec}f} ({(hi - lo) / a_h1:.1f} x ATR H1)",
         f"✅ Première cassure {'haussière' if d == 'BUY' else 'baissière'} avec clôture forte",
         "✅ Sens aligné avec l'EMA 50 du H4"],
        lines=[{"y": hi, "label": "Asie H", "kind": "range"}, {"y": lo, "label": "Asie B", "kind": "range"}],
        zones=[{"lo": lo, "hi": hi, "label": "Plage asiatique", "kind": "fib"}], inval=None)


# ==========================================================================
# S3  Sweep + CHoCH (prise de liquidité puis cassure de structure)
# ==========================================================================
def liquidity_levels(h1c, d1, m15c, now, a_h1):
    """[(nom, prix, 'high'|'low')] : PDH/PDL, extrêmes de la plage asiatique, equal highs/lows H1."""
    out = []
    if d1 and len(d1) >= 3:
        out += [("PDH", d1[-2]["high"], "high"), ("PDL", d1[-2]["low"], "low")]
    a0, a1 = asia_window(now)
    if now >= a1:
        asia = [c for c in m15c if a0 <= c["epoch"] < a1]
        if len(asia) >= 20:
            out += [("Asie H", max(c["high"] for c in asia), "high"), ("Asie B", min(c["low"] for c in asia), "low")]
    for kind, nm in (("high", "EQH"), ("low", "EQL")):
        lv = [h1c[i][kind] for i in swings(h1c, kind, 2)][-8:]
        for x in range(len(lv)):
            hit = next((y for y in range(x + 1, len(lv)) if abs(lv[x] - lv[y]) <= S3_EQ_TOL_ATR * a_h1), None)
            if hit is not None:
                out.append((nm, max(lv[x], lv[hit]) if kind == "high" else min(lv[x], lv[hit]), kind))
    return out


def scan_s3(provide, now, funnel, dec=2, kz=True):
    d1, h4, h1, m15, m5 = (provide(x) for x in ("D1", "H4", "H1", "M15", "M5"))
    if not (h1 and m15 and m5 and h4):
        return None
    h1c, m15c, m5c = h1[:-1], m15[:-1], m5[:-1]
    if len(h1c) < 40 or len(m15c) < 40 or len(m5c) < 60:
        return None
    a_h1, a_m15, a_m5 = atr(h1c), atr(m15c), atr(m5c)
    if min(a_h1, a_m15, a_m5) <= 0:
        return None
    levels = liquidity_levels(h1c, d1, m15c, now, a_h1)
    if not levels:
        return None
    funnel["S3_niveau"] += 1
    best = None
    for name, px, kind in levels:
        for i in range(max(0, len(m15c) - S3_SWEEP_BARS), len(m15c)):
            c = m15c[i]
            swept = (c["high"] > px and c["close"] < px) if kind == "high" else (c["low"] < px and c["close"] > px)
            if not swept:
                continue
            later = m15c[i + 1:]
            if any((x["close"] > px) if kind == "high" else (x["close"] < px) for x in later):
                continue                              # le niveau a été franchi pour de bon
            if best is None or c["epoch"] > best[0]["epoch"]:
                best = (c, name, px, kind)
    if best is None:
        return None
    funnel["S3_sweep"] += 1
    sc, name, px, kind = best
    d = "SELL" if kind == "high" else "BUY"
    after = [(i, c) for i, c in enumerate(m5c) if c["epoch"] >= sc["epoch"]]
    if len(after) < 3:
        return None
    ext_i, ext = (max(after, key=lambda x: (x[1]["high"], x[0])) if d == "SELL"
                  else min(after, key=lambda x: (x[1]["low"], -x[0])))
    ext_px = ext["high"] if d == "SELL" else ext["low"]
    pk = "low" if d == "SELL" else "high"
    piv = [i for i in swings(m5c, pk, 2) if ext_i - S3_PIVOT_BARS <= i < ext_i]
    if not piv:
        return None
    pivot = m5c[piv[-1]][pk]
    avg = body_avg(m5c[-21:-1])
    trig = next((i for i in range(ext_i + 1, len(m5c))
                 if ((m5c[i]["close"] < pivot) if d == "SELL" else (m5c[i]["close"] > pivot))), None)
    if trig is None or trig < len(m5c) - FRESH:
        return None
    tc = m5c[trig]
    if body(tc) < DISP_MULT * avg:
        return None
    sess = session_name(tc["epoch"])
    if kz and sess not in S3_SESSIONS:
        return None
    price = m5[-1]["close"]
    if is_late(d, price, tc["close"], a_m5):
        funnel["stale"] += 1
        return None
    sl = ext_px + BUF_ATR * a_m15 if d == "SELL" else ext_px - BUF_ATR * a_m15
    lv = build_levels(d, price, sl, a_m5, a_h1, S3_RISK_CAP_H1)
    if lv is None:
        return None
    funnel["S3_signal"] += 1
    opp = [(n, p) for n, p, k in levels if (k == "low" if d == "SELL" else k == "high")
           and abs(p - price) >= lv["risk"]
           and ((p < price) if d == "SELL" else (p > price))]
    opp.sort(key=lambda x: abs(x[1] - price))
    bias = h4_ema_bias(h4)
    aligned = bias == (-1 if d == "SELL" else 1)
    details = [f"✅ Liquidité prise : {name} {px:.{dec}f} (mèche {ext_px:.{dec}f})",
               f"✅ CHoCH M5 sous {pivot:.{dec}f}" if d == "SELL" else f"✅ CHoCH M5 au-dessus de {pivot:.{dec}f}",
               f"✅ Killzone {sess}",
               "✅ Aligné avec l'EMA 50 du H4" if aligned else "⚠️ Contre l'EMA 50 du H4 (contre-tendance)"]
    if opp:
        details.append(f"Liquidité opposée  {opp[0][0]} {opp[0][1]:.{dec}f}")
    return _signal(
        "S3", "Sweep + CHoCH", d, price, lv, m5, "M5", f"{sc['epoch']}|{tc['epoch']}", details,
        lines=[{"y": px, "label": name, "kind": "range"}, {"y": ext_px, "label": "Sweep", "kind": "sweep"},
               {"y": pivot, "label": "CHoCH", "kind": "info"}],
        inval=ext_px, session=sess, extra={"liq": name, "h4": "aligné" if aligned else "contre"})


# ==========================================================================
# S4  Mean Reversion (marché sans tendance)
# ==========================================================================
def scan_s4(provide, now, funnel, dec=2, kz=True):
    h1, m15 = provide("H1"), provide("M15")
    if not (h1 and m15):
        return None
    h1c, m15c = h1[:-1], m15[:-1]
    if len(h1c) < 70 or len(m15c) < 60:
        return None
    a = adx(h1c)
    closes_h1 = [c["close"] for c in h1c]
    e50 = ema(closes_h1, 50)
    a_h1, a_m15 = atr(h1c), atr(m15c)
    if a is None or e50[-1] is None or e50[-13] is None or a_h1 <= 0 or a_m15 <= 0:
        return None
    if a >= S4_ADX_MAX or abs(e50[-1] - e50[-13]) >= S4_FLAT_ATR * a_h1:
        return None
    funnel["S4_regime"] += 1
    k = len(m15c) - 1
    for i in range(k, k - FRESH, -1):                 # bougie de rejet : l'une des 2 dernières clôturées
        c = m15c[i]
        if kz and not in_hours(c["epoch"], S4_HOURS):
            continue
        closes = [x["close"] for x in m15c[:i + 1]]
        bb = bollinger(closes, i)
        rsis = rsi_series(closes, S4_RSI_N)
        if bb is None or rsis[-1] is None:
            continue
        lo_b, mid, up_b = bb
        recent_rsi = [r for r in rsis[-2:] if r is not None]
        if c["low"] < lo_b and c["close"] > lo_b and c["close"] > c["open"] and min(recent_rsi) <= S4_RSI_LOW:
            d = "BUY"
        elif c["high"] > up_b and c["close"] < up_b and c["close"] < c["open"] and max(recent_rsi) >= S4_RSI_HIGH:
            d = "SELL"
        else:
            continue
        price = m15[-1]["close"]
        if is_late(d, price, c["close"], a_m15):
            funnel["stale"] += 1
            return None
        ext = c["low"] if d == "BUY" else c["high"]
        sl = ext - S4_BUF_ATR * a_m15 if d == "BUY" else ext + S4_BUF_ATR * a_m15
        far = up_b if d == "BUY" else lo_b
        lv = build_levels(d, price, sl, a_m15, a_h1, S4_RISK_CAP_H1, tps=[mid, far, None])
        if lv is None:
            return None
        gain = lambda t: ((t - price) if d == "BUY" else (price - t)) / lv["risk"]
        if gain(mid) < S4_MIN_RR1 or gain(far) < S4_MIN_RR2:
            return None
        funnel["S4_signal"] += 1
        return _signal(
            "S4", "Mean Reversion", d, price, lv, m15, "M15", f"{c['epoch']}",
            [f"✅ Range H1 (ADX {a:.0f}, EMA 50 plate)",
             f"✅ Rejet de la bande de Bollinger {'basse' if d == 'BUY' else 'haute'} (RSI {min(recent_rsi) if d == 'BUY' else max(recent_rsi):.0f})",
             f"✅ Heure favorable ({session_name(c['epoch'])})"],
            lines=[{"y": mid, "label": "Moyenne", "kind": "info"}, {"y": ext, "label": "Rejet", "kind": "sweep"}],
            zones=[{"lo": lo_b, "hi": up_b, "label": "Bollinger", "kind": "fib"}], inval=ext)
    return None


STRATEGIES = [("S1", scan_s1), ("S2", scan_s2), ("S3", scan_s3), ("S4", scan_s4)]


def scan_all(provide, now, funnel, only=None, dec=2, kz=True):
    """Évalue les stratégies (toutes, ou celles de `only`) et renvoie les signaux trouvés.
    dec : décimales du prix de l'actif (affichage uniquement).
    kz : False pour un actif 24/7 (cryptos) : S3 et S4 ne sont alors pas limitées aux heures de session."""
    out = []
    for name, fn in STRATEGIES:
        if only and name not in only:
            continue
        sig = fn(provide, now, funnel, dec, kz) if name in ("S3", "S4") else fn(provide, now, funnel, dec)
        if sig:
            funnel["signal"] += 1
            out.append(sig)
    return out
