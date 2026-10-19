"""Suivi des trades du bot or : ordre en attente -> position ouverte -> résultat en R.

Le module est autonome (aucun import de bot.py) : il reçoit des bougies et renvoie des événements.
Gestion appliquée au suivi et au backtest (tout est réglable ci-dessous) :
  - ordre en attente annulé si TP1 est atteint avant exécution, si une bougie clôture au-delà du stop,
    ou si l'ordre dépasse sa durée de validité ;
  - à TP1 : clôture de PARTIAL_AT_TP1 de la position, stop remonté à l'entrée (BE_AT_TP1) ;
  - à TP2 : clôture de PARTIAL_AT_TP2, stop remonté à TP1 (LOCK_AT_TP2), le reste vise TP3
    (PARTIAL_AT_TP2 = 0 : tout est clôturé à TP2) ;
  - avant TP1, sortie si une bougie clôture au-delà du niveau d'invalidation (bord de l'OB ou de la zone
    d'entrée) : la structure est cassée (STRUCT_EXIT) ;
  - avant TP1, sortie au prix si le trade traîne plus de TIME_STOP_BARS bougies LTF ;
  - si le stop et une cible sont touchés dans la même bougie, le stop est supposé touché en premier.
"""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PARTIAL_AT_TP1 = 0.5      # part de la position clôturée à TP1
PARTIAL_AT_TP2 = 0.25     # part clôturée à TP2 ; le reste (25 %) vise TP3. 0 = tout à TP2
BE_AT_TP1 = True          # stop remonté à l'entrée une fois TP1 atteint
LOCK_AT_TP2 = True        # stop remonté à TP1 une fois TP2 atteint
STRUCT_EXIT = True        # sortie si la structure casse contre le trade avant TP1
TIME_STOP_BARS = 150      # sortie si TP1 n'est pas atteint après 150 bougies LTF (0 = désactivé)
KEEP_CLOSED = 1000        # nombre de trades clôturés conservés
ACTIVE = ("pending", "open")


def load_trades(path="trades.json"):
    try:
        with open(path) as f:
            d = json.load(f)
        d.setdefault("active", {})
        d.setdefault("closed", [])
        for t in d["active"].values():      # trades enregistrés par une version précédente
            hit = t.get("tp1_hit", False)
            t.setdefault("stage", 1 if hit else 0)
            t.setdefault("rem", 1 - PARTIAL_AT_TP1 if hit else 1.0)
            t.setdefault("r_acc", PARTIAL_AT_TP1 * (t.get("r1") or 0) if hit else 0.0)
            t.setdefault("inval", None)
            t.setdefault("deadline", None)
        return d
    except Exception:
        return {"active": {}, "closed": []}


def save_trades(tr, path="trades.json"):
    tr["closed"] = tr["closed"][-KEEP_CLOSED:]
    with open(path, "w") as f:
        json.dump(tr, f)


def new_trade(bot, key, sym, name, d, otype, entry, sl, tp1, tp2, tp3, ltf, gran, dec,
              cascade, now, ttl, extra=None, inval=None, bar_secs=None):
    """Crée l'enregistrement d'un trade. Un ordre au marché (BUY/SELL) est exécuté tout de suite.
    inval : niveau d'invalidation de la structure ; bar_secs : durée d'une bougie LTF (délai du time stop)."""
    buy = d == "BUY"
    market = otype in ("BUY", "SELL")
    if tp1 is not None and ((buy and tp1 <= entry) or (not buy and tp1 >= entry)):
        tp1 = None  # TP1 déjà dépassé à l'entrée : pas de clôture partielle
    t = {"bot": bot, "key": key, "sym": sym, "name": name, "dir": d, "otype": otype,
         "entry": entry, "sl": sl, "sl_now": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
         "ltf": ltf, "gran": gran, "dec": dec, "cascade": cascade, "t0": int(now), "ttl": ttl,
         "status": "open" if market else "pending", "stage": 0, "rem": 1.0, "r_acc": 0.0,
         "tp1_hit": False, "r1": None, "inval": inval,
         "deadline": int(now + TIME_STOP_BARS * (bar_secs or gran)) if TIME_STOP_BARS else None,
         "r": None, "result": None, "seen": 0, "t_close": None}
    if extra:
        t.update(extra)
    return t


def risk_of(t):
    return abs(t["entry"] - t["sl"])


def r_of(t, price):
    """Gain en multiples du risque initial si la position est clôturée à `price`."""
    risk = risk_of(t)
    if risk <= 0:
        return 0.0
    gain = (price - t["entry"]) if t["dir"] == "BUY" else (t["entry"] - price)
    return gain / risk


def _start_epoch(t):
    """Première bougie à analyser : celle de la création pour un ordre en attente (le prix n'a pas
    pu toucher l'entrée avant l'alerte), la suivante pour un ordre au marché."""
    g, t0 = t["gran"], t["t0"]
    if t["otype"] in ("BUY", "SELL"):
        return -(-t0 // g) * g
    return t0 // g * g


def _close(t, r, result, now):
    t["status"], t["r"], t["result"], t["t_close"] = "closed", round(r, 3), result, int(now)


def advance(t, candles, now):
    """Fait avancer le trade avec les bougies récentes. Retourne la liste des événements :
    'filled', 'tp1', 'tp2', 'tp3', 'sl', 'be', 'lock', 'struct', 'time', 'cancel_tp', 'cancel_sl', 'expired'."""
    ev = []
    if t["status"] not in ACTIVE:
        return ev
    buy, g = t["dir"] == "BUY", t["gran"]
    up = (lambda c, p: c["high"] >= p) if buy else (lambda c, p: c["low"] <= p)    # cible touchée
    down = (lambda c, p: c["low"] <= p) if buy else (lambda c, p: c["high"] >= p)  # stop touché
    start, last_closed = _start_epoch(t), t["seen"]
    for c in sorted(candles, key=lambda x: x["epoch"]):
        if c["epoch"] < start or c["epoch"] + g <= t["seen"]:
            continue
        closed = c["epoch"] + g <= now
        if t["status"] == "pending":
            fill = {"BUY LIMIT": c["low"] <= t["entry"], "BUY STOP": c["high"] >= t["entry"],
                    "SELL LIMIT": c["high"] >= t["entry"], "SELL STOP": c["low"] <= t["entry"]}[t["otype"]]
            if fill:
                t["status"] = "open"
                ev.append("filled")
            else:
                if t["tp1"] is not None and up(c, t["tp1"]):
                    t["status"], t["result"], t["t_close"] = "cancelled", "cancel_tp", int(now)
                    ev.append("cancel_tp")
                    break
                if closed and ((c["close"] < t["sl"]) if buy else (c["close"] > t["sl"])):
                    t["status"], t["result"], t["t_close"] = "cancelled", "cancel_sl", int(now)
                    ev.append("cancel_sl")
                    break
                if closed:
                    last_closed = c["epoch"] + g
                continue
        # position ouverte : un stop remonté ne s'applique qu'aux bougies suivant l'événement qui l'a remonté
        stop_active = not (t["stage"] > 0 and c["epoch"] <= t.get("be_from", 0))
        if stop_active and down(c, t["sl_now"]):
            _close(t, t["r_acc"] + t["rem"] * r_of(t, t["sl_now"]),
                   ("SL", "TP1+BE", "TP2+stop")[t["stage"]], now)
            ev.append(("sl", "be", "lock")[t["stage"]])
            break
        if closed and t["stage"] == 0:
            if STRUCT_EXIT and t.get("inval") is not None and \
                    ((c["close"] < t["inval"]) if buy else (c["close"] > t["inval"])):
                _close(t, r_of(t, c["close"]), "struct", now)
                ev.append("struct")
                break
            if t.get("deadline") and c["epoch"] + g >= t["deadline"]:
                _close(t, r_of(t, c["close"]), "time", now)
                ev.append("time")
                break
        if t["stage"] == 0 and t["tp1"] is not None and up(c, t["tp1"]):
            r1 = r_of(t, t["tp1"])
            part = min(PARTIAL_AT_TP1, t["rem"])
            t["r_acc"] += part * r1
            t["rem"] -= part
            t["tp1_hit"], t["r1"], t["stage"], t["be_from"] = True, r1, 1, c["epoch"]
            if BE_AT_TP1:
                t["sl_now"] = t["entry"]
            ev.append("tp1")
        if t["stage"] < 2 and up(c, t["tp2"]):
            r2 = r_of(t, t["tp2"])
            if PARTIAL_AT_TP2 > 0 and t.get("tp3") is not None:
                part = min(PARTIAL_AT_TP2, t["rem"])
                t["r_acc"] += part * r2
                t["rem"] -= part
                t["stage"], t["be_from"] = 2, c["epoch"]
                if LOCK_AT_TP2:
                    t["sl_now"] = t["tp1"] if t["tp1"] is not None else t["entry"]
                ev.append("tp2")
            else:
                _close(t, t["r_acc"] + t["rem"] * r2, "TP2", now)
                ev.append("tp2")
                break
        if t["stage"] == 2 and up(c, t["tp3"]):
            _close(t, t["r_acc"] + t["rem"] * r_of(t, t["tp3"]), "TP3", now)
            ev.append("tp3")
            break
        if closed:
            last_closed = c["epoch"] + g
    t["seen"] = max(t["seen"], last_closed)
    if t["status"] == "pending" and now - t["t0"] > t["ttl"]:
        t["status"], t["result"], t["t_close"] = "expired", "expired", int(now)
        ev.append("expired")
    return ev


def archive(tr, key):
    """Déplace un trade terminé de 'active' vers 'closed' (les ordres annulés/expirés ne comptent pas)."""
    t = tr["active"].pop(key, None)
    if t and t["status"] == "closed":
        tr["closed"].append(t)


def active_list(tr):
    return [t for t in tr["active"].values() if t["status"] in ACTIVE]


def find_trade(tr, tid):
    """Retrouve un trade par son numéro (actif ou clôturé)."""
    for t in list(tr["active"].values()) + tr["closed"][::-1]:
        if t.get("id") == tid:
            return t
    return None


def day_start(now):
    """Début de la journée de trading : 17h New York."""
    dt = datetime.fromtimestamp(now, NY)
    s = dt.replace(hour=17, minute=0, second=0, microsecond=0)
    if dt < s:
        s -= timedelta(days=1)
    return int(s.timestamp())


def realized_r(tr, since):
    return sum(t["r"] for t in tr["closed"] if t.get("t_close", 0) >= since and t.get("r") is not None)


# --------------------------------------------------------------------------
# Statistiques
# --------------------------------------------------------------------------
def summarize(ts):
    rs = [t["r"] for t in ts if t.get("r") is not None]
    if not rs:
        return None
    wins, loss = [r for r in rs if r > 0], [r for r in rs if r < 0]
    peak = dd = cum = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": len(rs), "win": len(wins) / len(rs) * 100, "avg": sum(rs) / len(rs),
            "total": sum(rs), "dd": dd,
            "pf": (sum(wins) / -sum(loss)) if loss else float("inf")}


def fmt_summary(label, s):
    pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"{label} : {s['n']} trades · {s['win']:.0f} % gagnants · {s['total']:+.1f} R "
            f"(moy {s['avg']:+.2f} R) · PF {pf} · DD max {s['dd']:.1f} R")


def taken_label(t):
    return {True: "pris", False: "ignoré"}.get(t.get("taken"), "non renseigné")


def stats_text(tr, now, days=30, groups=("bot", "cascade")):
    ts = [dict(t, statut=taken_label(t)) for t in tr["closed"] if t.get("t_close", 0) >= now - days * 86400]
    s = summarize(ts)
    if not s:
        return f"Aucun trade clôturé sur {days} jours."
    lines = [fmt_summary(f"Bilan {days} j", s)]
    for g in groups:
        for val in sorted({t.get(g) for t in ts if t.get(g)}):
            sub = summarize([t for t in ts if t.get(g) == val])
            if sub:
                lines.append(fmt_summary(f"· {val}", sub))
    if any(t["statut"] != "non renseigné" for t in ts):
        for val in ("pris", "ignoré", "non renseigné"):
            sub = summarize([t for t in ts if t["statut"] == val])
            if sub:
                lines.append(fmt_summary(f"· {val}", sub))
    own = [t for t in ts if t.get("own_r") is not None]
    if own:
        lines.append(f"Mes résultats réels : {len(own)} trades · {sum(t['own_r'] for t in own):+.1f} R "
                     f"(simulé sur les mêmes trades : {sum(t['r'] for t in own):+.1f} R)")
    return "\n".join(lines)
