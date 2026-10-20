"""Garde-fous du bot (or, paires majeures, BTC et ETH) : fenêtres sans alerte (news, rollover, week-end),
un seul trade actif par actif, stratégie et sens, exposition USD corrélée, plafond de pertes
journalier, calcul du lot, surveillance du bot, entonnoir de filtres et bilan hebdomadaire.
Autonome : aucun import de bot.py (les fonctions d'envoi sont passées en argument)."""
import json, math, os, re
from datetime import datetime
from zoneinfo import ZoneInfo

import tracker as T

NY = ZoneInfo("America/New_York")
GUARD_FILE = "guard.json"
NEWS_FILE = "news.json"

# --- Limites de risque ---
MAX_OPEN_TRADES = 8        # positions ouvertes en même temps (tous actifs et stratégies confondus)
GROUP_LIMIT = 2            # exposition nette maximale par groupe corrélé (USD, cryptos), même sens
MAX_DAILY_LOSS_R = 3.0     # plus de nouvelles alertes si la perte du jour atteint 3 R (jour = 17h NY)
# --- Fenêtres sans alerte (forex et or) ---
ROLLOVER = (16 * 60 + 55, 17 * 60 + 15)     # minutes NY : élargissement des spreads
FRIDAY_CLOSE_FROM = 15 * 60 + 30            # vendredi : trop proche de la fermeture hebdomadaire
SUNDAY_OPEN = (17 * 60, 18 * 60)            # dimanche : ouverture de semaine (gap)
NFP = (8 * 60, 9 * 60 + 30)                 # premier vendredi du mois, publication à 8h30 NY
# --- Calendrier économique automatique (flux gratuit Forex Factory / Fair Economy) ---
CALENDAR_URLS = [u for u in (
    os.getenv("CALENDAR_URL"),                                   # pour forcer une autre source
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
) if u]
CALENDAR_REFRESH = 6 * 3600     # le flux n'est interrogé qu'une fois toutes les 6 h
CALENDAR_RETRY = 900            # nouvel essai après 15 min en cas d'échec
CALENDAR_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"}
NEWS_BEFORE_MIN = 15            # pas d'alerte 15 min avant un événement à fort impact
NEWS_AFTER_MIN = 45             # ... ni 45 min après
BIG_AFTER_MIN = 90              # 90 min après une décision de taux ou une conférence de presse
BIG_EVENT = re.compile(r"rate|fomc|press conference|monetary policy|statement|minutes", re.I)
# Décisions de taux de la Fed 2026 (14h ET, conférence de presse à 14h30). Source : calendrier officiel de la Fed.
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
             "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]
# --- Surveillance ---
GAP_ALERT_MIN = 30         # alerte si le bot reprend après plus de 30 min sans scan
FAILS_ALERT = 3            # alerte après 3 scans de suite sans aucune donnée Deriv
REPORT_EVERY = 7 * 86400   # bilan envoyé une fois par semaine

FUNNEL_ORDER = ["data", "S1_tendance", "S1_repli", "S1_signal", "S2_range", "S2_signal", "S3_niveau", "S3_sweep",
                "S3_signal", "S4_regime", "S4_signal", "signal", "stale", "blackout", "dedupe", "risk", "alert"]
FUNNEL_LABEL = {"data": "données", "S1_tendance": "Trend Pullback : tendance H4", "S1_repli": "repli H1", "S1_signal": "signal Trend Pullback",
                "S2_range": "Asian Breakout : plage", "S2_signal": "signal Asian Breakout", "S3_niveau": "Sweep + CHoCH : niveaux",
                "S3_sweep": "sweep", "S3_signal": "signal Sweep + CHoCH", "S4_regime": "Mean Reversion : range H1", "S4_signal": "signal Mean Reversion",
                "signal": "signaux valides", "stale": "écartés (entrée tardive)", "blackout": "bloqués (news/horaire)",
                "dedupe": "doublons", "risk": "bloqués (risque)", "alert": "alertes"}


def load_guard(path=GUARD_FILE):
    try:
        with open(path) as f:
            g = json.load(f)
    except Exception:
        g = {}
    g.setdefault("health", {})
    g.setdefault("funnel", {})
    g.setdefault("last_report", 0)
    g.setdefault("paused_since", 0)
    return g


def save_guard(g, path=GUARD_FILE):
    with open(path, "w") as f:
        json.dump(g, f)


# --------------------------------------------------------------------------
# Fenêtres sans alerte : rollover, week-end, NFP, calendrier perso (news.json)
# --------------------------------------------------------------------------
def load_news(path=NEWS_FILE):
    """news.json : [{"start": "2026-10-28T18:00:00Z", "end": "2026-10-28T19:30:00Z",
    "note": "FOMC", "currencies": ["USD"]}] ; 'currencies' est facultatif (sinon tous les actifs réels)."""
    try:
        with open(path) as f:
            out = []
            for n in json.load(f):
                p = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
                out.append({"start": p(n["start"]), "end": p(n["end"]),
                            "note": n.get("note", "news"),
                            "cur": [c.upper() for c in n.get("currencies", [])]})
            return out
    except Exception:
        return []


def fomc_events():
    """Fenêtres FOMC intégrées : de 13h45 à 16h00 New York les jours de décision."""
    out = []
    for d in FOMC_2026:
        day = datetime.fromisoformat(d)
        t = lambda h, m: day.replace(hour=h, minute=m, tzinfo=NY).timestamp()
        out.append({"start": t(13, 45), "end": t(16, 0), "note": "FOMC", "cur": ["USD"]})
    return out


def fomc_check(g, now, notify):
    """Prévient (au plus tous les 14 jours) quand il reste moins de 30 jours de dates FOMC dans le code."""
    last = max(datetime.fromisoformat(d).replace(hour=16, tzinfo=NY).timestamp() for d in FOMC_2026)
    if last - now > 30 * 86400 or now - g.get("fomc_warned", 0) < 14 * 86400:
        return
    g["fomc_warned"] = int(now)
    if last < now:
        notify("⚠️ Plus aucune date FOMC dans guard.py (FOMC_2026). Ajoute les décisions de l'année à venir. "
               "Le calendrier automatique reste actif, mais ce filet de sécurité est vide.")
    else:
        notify(f"⚠️ La dernière date FOMC de guard.py (FOMC_2026) est le {max(FOMC_2026)}. "
               f"Ajoute bientôt les décisions de l'année suivante.")


def _parse_calendar(data):
    """Événements à fort impact des 8 grandes devises -> fenêtres sans alerte."""
    out = []
    for it in data if isinstance(data, list) else []:
        try:
            cur = str(it.get("country", "")).upper()
            if str(it.get("impact", "")).lower() != "high" or cur not in CALENDAR_CURRENCIES:
                continue
            ts = datetime.fromisoformat(str(it["date"]).replace("Z", "+00:00")).timestamp()
            title = str(it.get("title", "événement"))
            after = BIG_AFTER_MIN if BIG_EVENT.search(title) else NEWS_AFTER_MIN
            out.append({"start": ts - NEWS_BEFORE_MIN * 60, "end": ts + after * 60,
                        "note": f"{cur} {title}", "cur": [cur]})
        except Exception:
            continue
    return out


def calendar_events(g, now, http_get=None):
    """Fenêtres sans alerte issues du calendrier automatique + des décisions FOMC intégrées.
    Le flux est rafraîchi toutes les 6 h ; en cas d'échec, les dernières données restent utilisées."""
    cal = g.setdefault("calendar", {"fetched": 0, "events": []})
    if now - cal["fetched"] >= CALENDAR_REFRESH:
        events, ok = [], False
        for url in CALENDAR_URLS:
            try:
                if http_get is None:
                    import requests
                    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; gold-bot/1.0)"})
                    r.raise_for_status()
                    data = r.json()
                else:
                    data = http_get(url)
                events += _parse_calendar(data)
                ok = True
            except Exception as e:
                print(f"Calendrier indisponible ({url.rsplit('/', 1)[-1]}) : {str(e)[:80]}")
        if ok:
            cal["events"], cal["fetched"] = events, int(now)
            print(f"Calendrier mis à jour : {len(events)} événement(s) à fort impact.")
        else:
            cal["fetched"] = int(now) - CALENDAR_REFRESH + CALENDAR_RETRY
    upcoming = [e for e in cal["events"] if e["end"] >= now - 3600]
    return upcoming + fomc_events()


def blackout(sym, now, news=()):
    """Motif de blocage si l'alerte tombe dans une fenêtre à éviter, sinon None."""
    for n in news:
        if n["start"] <= now <= n["end"] and (not n["cur"] or any(c in sym.upper() for c in n["cur"])):
            return f"news : {n['note']}"
    if not sym.startswith("frx"):
        return None                    # cryptos : marché 24/7, pas de rollover ni de fermeture hebdomadaire
    dt = datetime.fromtimestamp(now, NY)
    mins, wd = dt.hour * 60 + dt.minute, dt.weekday()
    if ROLLOVER[0] <= mins < ROLLOVER[1]:
        return "rollover 17h NY"
    if wd == 4 and mins >= FRIDAY_CLOSE_FROM:
        return "fermeture hebdomadaire proche"
    if wd == 6 and SUNDAY_OPEN[0] <= mins < SUNDAY_OPEN[1]:
        return "ouverture de semaine"
    if wd == 4 and dt.day <= 7 and NFP[0] <= mins < NFP[1]:
        return "NFP"
    return None


# --------------------------------------------------------------------------
# Doublons, exposition corrélée, plafond de pertes
# --------------------------------------------------------------------------
def usd_sign(sym, d):
    """+1 si le trade équivaut à être long USD, -1 s'il équivaut à être short USD, 0 sinon.
    L'or (XAUUSD) est coté en USD : acheter l'or revient à vendre l'USD."""
    s = sym.upper()
    if not (s.startswith("FRX") and len(s) >= 9):
        return 0
    base, quote = s[3:6], s[6:9]
    buy = 1 if d == "BUY" else -1
    if quote == "USD":
        return -buy
    if base == "USD":
        return buy
    return 0


def crypto_sign(sym, d):
    """+1 pour un achat de crypto, -1 pour une vente, 0 pour un autre actif (BTC et ETH évoluent ensemble)."""
    return (1 if d == "BUY" else -1) if sym.startswith("cry") else 0


def risk_check(tr, sym, d, now, strat=None):
    """Retourne (ok, motif). Contrôle : doublon (même actif, stratégie et sens), nombre de trades ouverts,
    exposition USD nette, plafond de perte journalier."""
    act = T.active_list(tr)
    if any(t["sym"] == sym and t["dir"] == d and t.get("cascade") == strat for t in act):
        return False, "dedupe"
    if len(act) >= MAX_OPEN_TRADES:
        return False, f"{MAX_OPEN_TRADES} trades déjà actifs"
    sign = usd_sign(sym, d)
    net = sum(usd_sign(t["sym"], t["dir"]) for t in act)
    if sign and net * sign >= GROUP_LIMIT:
        return False, "exposition USD (même sens) au maximum"
    csign = crypto_sign(sym, d)
    cnet = sum(crypto_sign(t["sym"], t["dir"]) for t in act)
    if csign and cnet * csign >= GROUP_LIMIT:
        return False, "exposition crypto (même sens) au maximum"
    if T.realized_r(tr, T.day_start(now)) <= -MAX_DAILY_LOSS_R:
        return False, f"perte journalière de {MAX_DAILY_LOSS_R:g} R atteinte"
    return True, ""


# --------------------------------------------------------------------------
# Taille de position (facultatif : variable d'environnement ACCOUNT_BALANCE)
# --------------------------------------------------------------------------
def contract_units(sym):
    """Unités par lot standard : 100 onces pour l'or, 100 000 pour une paire forex, 1 pour une crypto. Autre contrat :
    renseigne LOT_UNITS_<SYMBOLE> (ex. LOT_UNITS_FRXXAUUSD)."""
    env = os.getenv(f"LOT_UNITS_{sym.upper()}")
    if env:
        try:
            return float(env)
        except ValueError:
            return None
    if sym == "frxXAUUSD":
        return 100.0
    if sym.startswith("cry"):
        return 1.0
    return 100000.0 if sym.startswith("frx") else None


def lot_size(sym, entry, sl, balance=None, risk_pct=None):
    """Lot pour risquer risk_pct % du solde, ou None si inconnu. Le résultat est arrondi vers le bas
    au 0,01 ; il reste une estimation à vérifier sur ta plateforme."""
    try:
        balance = float(balance or os.getenv("ACCOUNT_BALANCE") or 0)
        risk_pct = float(risk_pct or os.getenv("RISK_PCT") or 1)
    except ValueError:
        return None
    units = contract_units(sym)
    dist = abs(entry - sl)
    if balance <= 0 or not units or dist <= 0:
        return None
    loss_per_lot = dist * units
    if sym.startswith("frx") and sym[6:9] != "USD":
        loss_per_lot /= entry          # perte exprimée en devise de cotation -> USD
    lot = math.floor(balance * risk_pct / 100 / loss_per_lot * 100 + 1e-9) / 100
    return lot


def lot_line(sym, entry, sl):
    bal = os.getenv("ACCOUNT_BALANCE")
    if not bal:
        return None
    lot = lot_size(sym, entry, sl)
    pct = os.getenv("RISK_PCT") or "1"
    if lot is None:
        return f"Lot  à calculer (contrat inconnu) · risque {pct} %"
    return f"Lot  {lot:.2f}  (risque {pct} % de {bal})" if lot >= 0.01 else "Lot  < 0,01 : stop trop large pour ce risque"


# --------------------------------------------------------------------------
# Surveillance du bot
# --------------------------------------------------------------------------
def health_start(g, bot, now, notify):
    h = g["health"].setdefault(bot, {"last_ok": 0, "fails": 0, "alerted": False})
    if h["last_ok"] and now - h["last_ok"] > GAP_ALERT_MIN * 60:
        notify(f"⚠️ {bot} : scan interrompu pendant {(now - h['last_ok']) / 3600:.1f} h, il reprend. "
               f"Les setups apparus pendant la coupure ont pu être manqués.")
    return h


def health_end(g, bot, got_data, now, notify):
    """got_data : au moins un symbole a renvoyé des bougies pendant ce scan."""
    h = g["health"].setdefault(bot, {"last_ok": 0, "fails": 0, "alerted": False})
    if got_data:
        if h["alerted"]:
            notify(f"✅ {bot} : les données Deriv sont de nouveau accessibles.")
        h.update(last_ok=int(now), fails=0, alerted=False)
    else:
        h["fails"] += 1
        if h["fails"] >= FAILS_ALERT and not h["alerted"]:
            notify(f"⚠️ {bot} : aucune donnée Deriv sur {h['fails']} scans de suite. "
                   f"Vérifie l'API Deriv ou le workflow.")
            h["alerted"] = True


# --------------------------------------------------------------------------
# Entonnoir de filtres et bilan
# --------------------------------------------------------------------------
def funnel_add(g, bot, counts):
    acc = g["funnel"].setdefault(bot, {})
    for k, v in counts.items():
        acc[k] = acc.get(k, 0) + v


def funnel_text(counts):
    parts = [f"{FUNNEL_LABEL[k]} {counts[k]}" for k in FUNNEL_ORDER if counts.get(k)]
    return " → ".join(parts) if parts else "aucune donnée"


def report_due(g, now):
    return now - g["last_report"] >= REPORT_EVERY


def build_report(g, tr, now):
    lines = ["📊 Bilan hebdomadaire", T.stats_text(tr, now, days=7), ""]
    lines.append(T.stats_text(tr, now, days=30))
    act = T.active_list(tr)
    lines += ["", f"Trades actifs : {len(act)}"]
    for bot, counts in sorted(g["funnel"].items()):
        lines.append(f"Entonnoir {bot} : {funnel_text(counts)}")
    return "\n".join(lines)


def report_sent(g, now):
    g["last_report"] = int(now)
    g["funnel"] = {}
