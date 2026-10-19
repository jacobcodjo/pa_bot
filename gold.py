"""Bot or (XAUUSD) : 4 stratégies, données Deriv, alertes Telegram, suivi des trades.

Stratégies (détail dans STRATEGIES.md) :
  S1 Tendance + repli · S2 Cassure de la plage asiatique · S3 Liquidité + CHoCH · S4 Retour à la moyenne
Un passage = un scan (à déclencher toutes les 5 minutes par un cron externe, voir README.md).
"""
import json, os, random, sys, time
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import websocket  # websocket-client

import charts
import guard as G
import strategies as S
import tracker as T

APP_ID = os.getenv("DERIV_APP_ID", "1089")
# 1) endpoint public Deriv (sans authentification), 2) ancien endpoint en secours.
# On peut forcer un endpoint avec la variable d'environnement DERIV_WS_URL.
WS_URLS = [u for u in (
    os.getenv("DERIV_WS_URL"),
    "wss://api.derivws.com/trading/v1/options/ws/public",
    f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
) if u]
UA = "Mozilla/5.0 (compatible; gold-bot/1.0)"
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "frxXAUUSD"
NAME = "XAUUSD"
BOT = "OR"
DEC = 2                                  # décimales de l'or
ENABLED = [x.strip().upper() for x in os.getenv("STRATEGIES", "S1,S2,S3,S4").split(",") if x.strip()]
GRAN = S.GRAN
COUNTS = {"M5": 300, "M15": 400}         # bougies téléchargées à chaque scan
COUNT_H1 = 2500                          # H1 : sert aussi à reconstruire H4 et D1 (17h New York), ~100 jours
STALE_DATA = 3 * GRAN["M5"]              # données plus vieilles que 15 min : marché fermé ou flux coupé
TRADE_TTL = 3600                         # durée de validité affichée (ordres au marché : sans effet)

MIN_INTERVAL = 0.6       # s entre deux requêtes (throttle)
MAX_RETRIES = 6
CACHE_FILE = "cache.json"
STATE_FILE = "state.json"
NY = ZoneInfo("America/New_York")


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# --------------------------------------------------------------------------
# Client Deriv robuste : throttle + backoff exponentiel + reconnexion auto
# --------------------------------------------------------------------------
class Deriv:
    def __init__(self):
        self.ws = None
        self.last = 0.0
        self.req_id = 0

    def _connect(self):
        self.close()
        for i in range(MAX_RETRIES):
            url = WS_URLS[i % len(WS_URLS)]
            name = url.split("?")[0]
            try:
                self.ws = websocket.create_connection(url, timeout=20, header=[f"User-Agent: {UA}"])
                print(f"Connecté à {name}")
                return
            except Exception as e:
                self._sleep_backoff(i, f"connexion {name}: {str(e)[:60]}")
        raise RuntimeError("Impossible de se connecter à Deriv")

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    @staticmethod
    def _sleep_backoff(attempt, why):
        delay = min(60, 2 ** attempt) + random.uniform(0, 1)  # jitter
        print(f"[retry {attempt+1}] {why} -> pause {delay:.1f}s")
        time.sleep(delay)

    def request(self, payload):
        for attempt in range(MAX_RETRIES):
            try:
                if self.ws is None:
                    self._connect()
                wait = MIN_INTERVAL - (time.time() - self.last)
                if wait > 0:
                    time.sleep(wait)
                self.req_id += 1
                payload["req_id"] = self.req_id
                self.ws.send(json.dumps(payload))
                self.last = time.time()
                while True:  # ignore les messages sans rapport
                    msg = json.loads(self.ws.recv())
                    if msg.get("req_id") == self.req_id:
                        break
                err = msg.get("error")
                if err:
                    code = err.get("code")
                    if code in ("RateLimit", "RateLimitExceeded"):
                        self._sleep_backoff(attempt + 2, "rate limit")
                        continue
                    if code in ("InvalidSymbol", "MarketIsClosed"):
                        print(f"Ignoré ({code}): {payload.get('ticks_history')}")
                        return None  # marché fermé / symbole indisponible
                    raise RuntimeError(f"{code}: {err.get('message')}")
                return msg
            except (websocket.WebSocketException, ConnectionError, OSError) as e:
                self.close()  # forcera une reconnexion propre
                self._sleep_backoff(attempt, f"socket: {e}")
        return None

    def candles(self, symbol, gran, count=300, end="latest"):
        msg = self.request({"ticks_history": symbol, "style": "candles", "granularity": gran,
                            "count": count, "end": end})
        return msg.get("candles") if msg else None


def get_candles(api, cache, symbol, gran, force=False, count=300):
    """Cache : on ne re-télécharge que si la dernière bougie est clôturée (ou si force)."""
    key = f"{symbol}_{gran}"
    c = cache.get(key)
    if c and not force and time.time() < c[-1]["epoch"] + gran:
        return c
    fresh = api.candles(symbol, gran, count=count)
    if fresh:
        cache[key] = fresh
        return fresh
    return c


def aggregate_ny(h1, hours):
    """Regroupe des H1 en bougies de `hours` heures ouvrant à 17h New York (D1 = 24, H4 = 4)."""
    out = {}
    for c in h1:
        dt = datetime.fromtimestamp(c["epoch"], NY)
        h = (dt.hour - 17) % 24
        start = int((dt.replace(minute=0, second=0, microsecond=0) - timedelta(hours=h % hours)).timestamp())
        b = out.get(start)
        if b is None:
            out[start] = {"epoch": start, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]}
        else:
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
    return list(out.values())


def tf_candles(api, cache, tf):
    """Bougies d'un timeframe ; la dernière est en formation. M5 et M15 sont toujours rafraîchis."""
    if tf in ("H1", "H4", "D1"):
        h1 = get_candles(api, cache, SYMBOL, GRAN["H1"], count=COUNT_H1)
        if not h1 or tf == "H1":
            return h1
        return aggregate_ny(h1, 24 if tf == "D1" else 4)
    return get_candles(api, cache, SYMBOL, GRAN[tf], force=True, count=COUNTS[tf])


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def fmt(x):
    return f"{x:.{DEC}f}"


def code(x):
    return f"<code>{fmt(x)}</code>"


def tg_api(method, payload=None, files=None):
    """Appel à l'API Telegram ; retourne le JSON de la réponse ou None en cas d'échec."""
    if not (TG_TOKEN and TG_CHAT):
        return None
    try:
        if files:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=payload, files=files, timeout=30)
        else:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=15)
        if not r.ok:
            print(f"Telegram {method} erreur {r.status_code}: {r.text[:150]}")
            return None
        return r.json()
    except Exception as e:
        print(f"Telegram injoignable ({method}): {str(e)[:100]}")
        return None


def notify(text, html=False, buttons=None):
    """Envoie un message ; retourne son identifiant (None si l'envoi échoue ou sans secrets Telegram)."""
    if not (TG_TOKEN and TG_CHAT):
        print("ATTENTION : TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID vide -> message NON envoyé :")
        print(text)
        return None
    payload = {"chat_id": TG_CHAT, "text": text}
    if html:
        payload["parse_mode"] = "HTML"
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    r = tg_api("sendMessage", payload)
    return r["result"]["message_id"] if r and r.get("ok") else None


def notify_photo(path, caption, html=True, buttons=None):
    """Envoie une image avec légende (1024 caractères au plus) ; retourne l'identifiant du message ou None."""
    if not (TG_TOKEN and TG_CHAT):
        return None
    payload = {"chat_id": TG_CHAT, "caption": caption}
    if html:
        payload["parse_mode"] = "HTML"
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    with open(path, "rb") as f:
        r = tg_api("sendPhoto", payload, files={"photo": f})
    return r["result"]["message_id"] if r and r.get("ok") else None


def ratio_txt(d, entry, sl, tp):
    """Ratio gain/risque d'une cible, ex. '   (1:2.0)'."""
    risk = abs(entry - sl)
    gain = (tp - entry) if d == "BUY" else (entry - tp)
    if risk <= 0:
        return ""
    return "   (dépassé)" if gain <= 0 else f"   (1:{gain / risk:.1f})"


def mgmt_line(sig):
    """Règle de gestion annoncée dans l'alerte (reflète les réglages de tracker.py)."""
    p1, p2 = T.PARTIAL_AT_TP1, T.PARTIAL_AT_TP2
    has3 = sig.get("tp3") is not None
    parts = []
    if p1:
        parts.append(f"{int(p1 * 100)} % à TP1" + (" (SL à l'entrée)" if T.BE_AT_TP1 else ""))
    if has3 and p2:
        parts.append(f"{int(p2 * 100)} % à TP2" + (" (SL à TP1)" if T.LOCK_AT_TP2 else ""))
    rest = round((1 - p1 - (p2 if has3 else 0)) * 100)
    if rest > 0:
        parts.append(f"{rest} % vers " + ("TP3" if has3 and p2 else "TP2"))
    if not parts:
        return None
    out = "Gestion : " + ", ".join(parts)
    exits = (["structure cassée"] if T.STRUCT_EXIT and sig.get("inval") is not None else []) + \
            ([f"TP1 non atteint en {T.TIME_STOP_BARS} bougies"] if T.TIME_STOP_BARS else [])
    return out + (". Sortie si " + " ou ".join(exits) if exits else "")


def gold_message(sig):
    d, c = sig["dir"], code
    tps = [("TP1", sig["tp1"]), ("TP2", sig["tp2"]), ("TP3", sig["tp3"])]
    lines = [f"{'🟢' if d == 'BUY' else '🔴'} {d} · {NAME} · {sig['ltf']} · {sig['label']}",
             f"Session : {sig['session']}", "",
             "────────────────", f"Prix actuel  {c(sig['entry'])}", "────────────────", "",
             f"Entrée  {c(sig['entry'])}  (au marché)", f"SL  {c(sig['sl'])}"]
    lines += [f"{n}  {c(v)}{ratio_txt(d, sig['entry'], sig['sl'], v)}" for n, v in tps if v is not None]
    lines += [""] + sig["details"]
    if sig.get("inval") is not None and T.STRUCT_EXIT:
        lines.append(f"Invalidation  {c(sig['inval'])}  (clôture au-delà = sortie)")
    extra = [x for x in (G.lot_line(SYMBOL, sig["entry"], sig["sl"]), mgmt_line(sig)) if x]
    if extra:
        lines += [""] + extra
    return "\n".join(lines)


def trade_event_message(t, e):
    c = lambda v: code(v)
    head = f"{t['name']} · {t['ltf']} · {t['cascade']}" + (f" #{t['id']}" if t.get("id") else "")
    closed = t["status"] == "closed" and t.get("r") is not None
    res = f"\nRésultat  {t['r']:+.1f} R" if closed else ""
    if e == "filled":
        return f"▶️ Ordre touché · {t['otype']} · {head}\nEntrée  {c(t['entry'])}"
    if e == "tp1":
        sl_txt = f"SL à l'entrée {c(t['entry'])}" if T.BE_AT_TP1 else "SL inchangé"
        return (f"🎯 TP1 atteint · {head}\nClôture de {int(T.PARTIAL_AT_TP1 * 100)} %, {sl_txt}\n"
                f"TP1  {c(t['tp1'])}")
    if e == "tp2":
        if closed:
            return f"✅ TP2 atteint · {head}{res}"
        lock = f"SL remonté à {c(t['sl_now'])}" if T.LOCK_AT_TP2 else "SL inchangé"
        return (f"🎯 TP2 atteint · {head}\nClôture de {int(T.PARTIAL_AT_TP2 * 100)} %, {lock}, le reste vise TP3\n"
                f"TP2  {c(t['tp2'])}")
    if e == "tp3":
        return f"🏁 TP3 atteint · {head}{res}"
    if e == "sl":
        return f"❌ Stop touché · {head}{res}"
    if e == "be":
        return f"➖ Stop à l'entrée touché après TP1 · {head}{res}"
    if e == "lock":
        return f"🔒 Stop remonté touché après TP2 · {head}{res}"
    if e == "struct":
        return (f"🔻 Structure cassée · {head}\nUne bougie a clôturé au-delà de {c(t['inval'])}. "
                f"Ferme la position.{res}")
    if e == "time":
        return (f"⏱ Sortie au temps · {head}\nTP1 non atteint en {T.TIME_STOP_BARS} bougies. "
                f"Ferme la position.{res}")
    return f"⚠️ ANNULER · {head}"


# --------------------------------------------------------------------------
# Suivi des trades, alertes, commandes Telegram
# --------------------------------------------------------------------------
def track_trades(api, tr, now):
    """Met à jour les trades : TP1 à TP3, stop, sorties de structure, sortie au temps."""
    for key, t in list(tr["active"].items()):
        if t["bot"] != BOT:
            continue
        since = max(t["seen"], t["t0"] // t["gran"] * t["gran"])
        need = min(5000, max(10, int((now - since) / t["gran"]) + 3))
        try:
            cs = api.candles(t["sym"], t["gran"], count=need)
        except Exception as ex:
            print(f"Suivi {t['name']} ignoré : {str(ex)[:80]}")
            continue
        if not cs:
            continue
        for e in T.advance(t, cs, now):
            notify(trade_event_message(t, e), html=True)
        if t["status"] not in T.ACTIVE:
            T.archive(tr, key)


def build_chart(sig, ltf_candles):
    lines = [{"y": sig["entry"], "label": "Entrée", "kind": "entry"}, {"y": sig["sl"], "label": "SL", "kind": "sl"}]
    lines += [{"y": sig[k], "label": k.upper(), "kind": "tp"} for k in ("tp1", "tp2", "tp3") if sig.get(k) is not None]
    return {"title": f"{NAME} · {sig['dir']} · {sig['ltf']} · {sig['label']}", "dec": DEC,
            "candles": charts.candles_slice(ltf_candles), "lines": lines + sig["lines"], "zones": sig["zones"]}


def emit_alert(sig, ltf_candles, tr, g, sent, funnel, now):
    """Filtres horaires, contrôles de risque, envoi (graphique + boutons Pris / Ignoré), enregistrement."""
    sent[sig["key"]] = int(now)   # un setup bloqué ou alerté n'est jamais réexaminé
    ok, why = G.risk_check(tr, SYMBOL, sig["dir"], now, sig["label"])
    if not ok:
        if why == "dedupe":
            funnel["dedupe"] += 1
        else:
            funnel["risk"] += 1
            print(f"Alerte bloquée {sig['label']} {sig['dir']} : {why}")
            ds = T.day_start(now)
            if why.startswith("perte journalière") and g.get("paused_since") != ds:
                g["paused_since"] = ds
                notify(f"⏸ Alertes en pause : {why}. Reprise à la prochaine journée de trading (17h New York).")
        return False
    funnel["alert"] += 1
    tid = g["next_id"] = g.get("next_id", 0) + 1
    text = f"{gold_message(sig)}\n\nRéf. #{tid}"
    buttons = [[{"text": "✅ Pris", "callback_data": f"take:{tid}"},
                {"text": "❌ Ignoré", "callback_data": f"skip:{tid}"}]]
    delivered = False
    if os.getenv("CHARTS", "1") != "0" and TG_TOKEN and TG_CHAT:
        path = f"chart_{tid}.png"
        try:
            charts.render(build_chart(sig, ltf_candles), path)
            if len(text) <= 1000:
                delivered = notify_photo(path, text, buttons=buttons) is not None
            else:   # légende trop longue : image avec titre, puis le texte complet avec les boutons
                delivered = notify_photo(path, text.split("\n")[0]) is not None
                if delivered:
                    notify(text, html=True, buttons=buttons)
        except Exception as ex:
            print(f"Graphique ignoré : {str(ex)[:100]}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    if not delivered:
        notify(text, html=True, buttons=buttons)
    extra = dict(sig["extra"], session=sig["session"], id=tid)
    tr["active"][sig["key"]] = T.new_trade(
        BOT, sig["key"], SYMBOL, NAME, sig["dir"], sig["otype"], sig["entry"], sig["sl"],
        sig["tp1"], sig["tp2"], sig["tp3"], sig["ltf"], sig["gran"], DEC, sig["label"], now, TRADE_TTL,
        extra=extra, inval=sig["inval"], bar_secs=sig["gran"])
    return True


HELP = ("Commandes :\n/pris N : le trade #N est pris\n/ignore N : le trade #N est ignoré\n"
        "/r N X : ton résultat réel du trade #N, en R (ex. /r 12 -0.8)\n/bilan : bilan des 30 derniers jours")


def _set_taken(tr, tid, taken):
    t = T.find_trade(tr, tid)
    if t is None:
        return None
    t["taken"] = taken
    return t


def handle_update(u, g, tr, now):
    cq, msg = u.get("callback_query"), u.get("message")
    chat = str((cq or {}).get("message", {}).get("chat", {}).get("id") if cq else (msg or {}).get("chat", {}).get("id"))
    if chat != str(TG_CHAT):
        return  # on n'écoute que ton chat
    if cq:
        action, _, num = (cq.get("data") or "").partition(":")
        if action in ("take", "skip") and num.isdigit():
            t = _set_taken(tr, int(num), action == "take")
            label = "✅ Pris · noté" if action == "take" else "❌ Ignoré · noté"
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"],
                                           "text": label if t else "Trade introuvable (trop ancien ?)"})
            if t:
                tg_api("editMessageReplyMarkup", {
                    "chat_id": TG_CHAT, "message_id": cq["message"]["message_id"],
                    "reply_markup": {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]}})
        else:
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"]})
        return
    parts = (msg.get("text") or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    if cmd in ("/pris", "/ignore") and args and args[0].lstrip("#").isdigit():
        t = _set_taken(tr, int(args[0].lstrip("#")), cmd == "/pris")
        notify(f"Trade #{args[0].lstrip('#')} noté « {'pris' if cmd == '/pris' else 'ignoré'} »." if t
               else "Trade introuvable (trop ancien ?).")
    elif cmd == "/r" and len(args) == 2 and args[0].lstrip("#").isdigit():
        try:
            val = float(args[1].replace(",", "."))
        except ValueError:
            notify("Format : /r N X, par exemple /r 12 -0.8")
            return
        t = _set_taken(tr, int(args[0].lstrip("#")), True)
        if t:
            t["own_r"] = val
            notify(f"Trade #{args[0].lstrip('#')} : ton résultat réel {val:+.2f} R est enregistré.")
        else:
            notify("Trade introuvable (trop ancien ?).")
    elif cmd == "/bilan":
        notify(T.stats_text(tr, now, days=30, groups=("cascade",)))
    else:
        notify(HELP)


def poll_telegram(g, tr, now):
    """Lit les boutons et commandes envoyés depuis le dernier scan (réponse sous 5 minutes)."""
    if not (TG_TOKEN and TG_CHAT):
        return
    r = tg_api("getUpdates", {"offset": g.get("tg_offset", 0), "timeout": 0,
                              "allowed_updates": ["callback_query", "message"]})
    if not r or not r.get("ok"):
        return
    for u in r.get("result", []):
        g["tg_offset"] = u["update_id"] + 1
        try:
            handle_update(u, g, tr, now)
        except Exception as ex:
            print(f"Message Telegram ignoré : {str(ex)[:100]}")


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------
def check_symbol(api):
    """Journalise si l'or est absent chez Deriv (diagnostic uniquement)."""
    try:
        msg = api.request({"active_symbols": "brief"})
        names = {x.get("symbol") or x.get("underlying_symbol") for x in (msg or {}).get("active_symbols", [])}
        if names:
            print(f"Symbole {SYMBOL} : {'disponible' if SYMBOL in names else 'ABSENT chez Deriv'}")
    except Exception as e:
        print(f"check_symbol ignoré: {e}")


def scan(provide, now, sent, funnel, news=()):
    """Évalue les stratégies actives. Retourne [(signal, bougies de l'unité d'entrée)] non encore envoyés.
    Utilisé par le bot en direct et par le backtest."""
    m5 = provide("M5")
    if not m5 or now - m5[-1]["epoch"] > STALE_DATA:
        return []                      # données absentes ou marché fermé
    funnel["data"] += 1
    out = []
    for sig in S.scan_all(provide, now, funnel, only=ENABLED):
        if sig["key"] in sent:
            continue
        why = G.blackout(SYMBOL, now, news)
        if why:
            funnel["blackout"] += 1
            sent[sig["key"]] = int(now)
            print(f"Alerte bloquée {sig['label']} {sig['dir']} : {why}")
            continue
        out.append((sig, provide(sig["ltf"])))
    return out


def main():
    if "--stats" in sys.argv:
        tr, g = T.load_trades(), G.load_guard()
        print(T.stats_text(tr, time.time(), days=30, groups=("cascade",)))
        for bot, counts in sorted(g["funnel"].items()):
            print(f"Entonnoir {bot} : {G.funnel_text(counts)}")
        return
    now = time.time()
    cache, state = load(CACHE_FILE), load(STATE_FILE)
    tr, g = T.load_trades(), G.load_guard()
    news = G.load_news() + G.calendar_events(g, now)
    api, funnel = Deriv(), Counter()
    if not (TG_TOKEN and TG_CHAT):
        print("ATTENTION : secret Telegram manquant ou vide (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)")
    try:
        G.health_start(g, BOT, now, notify)
        G.fomc_check(g, now, notify)
        poll_telegram(g, tr, now)
        check_symbol(api)
        track_trades(api, tr, now)
        memo = {}

        def provide(tf, force=False):          # un seul téléchargement par timeframe et par scan
            if tf not in memo:
                memo[tf] = tf_candles(api, cache, tf)
            return memo[tf]
        for sig, ltf in scan(provide, now, state, funnel, news):
            emit_alert(sig, ltf, tr, g, state, funnel, now)
        G.health_end(g, BOT, funnel["data"] > 0, now, notify)
        G.funnel_add(g, BOT, funnel)
        if not g["last_report"]:
            g["last_report"] = int(now)
        elif G.report_due(g, now):
            notify(G.build_report(g, tr, now))
            G.report_sent(g, now)
        if os.getenv("NOTIFY_OK") == "1":
            notify(f"Bot or OK : {funnel['alert']} alerte(s). Stratégies actives : {', '.join(ENABLED)}.\n"
                   f"Entonnoir : {G.funnel_text(funnel)}\n{T.stats_text(tr, now, groups=('cascade',))}")
        print(f"Scan terminé : {funnel['alert']} alerte(s). Entonnoir : {G.funnel_text(funnel)}")
    finally:
        api.close()
        cut = time.time() - 7 * 86400
        state = {k: v for k, v in state.items() if v > cut}
        save(CACHE_FILE, cache)
        save(STATE_FILE, state)
        T.save_trades(tr)
        G.save_guard(g)


if __name__ == "__main__":
    main()
