"""Backtest du bot : rejoue les 4 stratégies sur de l'historique M5 de l'or et des paires majeures.

Le rejeu appelle les mêmes fonctions de scan que le bot en direct (gold.scan), les mêmes garde-fous
(guard.risk_check, fenêtres sans alerte) et la même gestion des trades (tracker.advance). Toutes les
unités de temps sont reconstruites depuis le M5, sans regarder le futur : à chaque pas, seules les
bougies clôturées sont visibles et la bougie en formation est partielle.

Outils de validation (pour ne pas se fier à un seul chiffre) :
  - rapport par stratégie, par sens, par session, par alignement H4 ;
  - séparation en deux périodes (--split 0.6) : les mêmes règles doivent tenir sur la seconde ;
  - intervalle de confiance à 95 % de l'espérance par trade (bootstrap) et verdict prudent ;
  - grille de réglages (--set strategies.S1_ADX_MIN=15,18,22) avec le résultat de chaque période
    pour repérer un réglage qui ne marche que sur une seule période (sur-ajustement).

Exemples :
  python backtest.py --days 120 --split 0.6                          (or seul, par défaut)
  python backtest.py --sym frxXAUUSD,frxEURUSD,frxGBPUSD --days 120 --split 0.6
  python backtest.py --sym all --days 120 --split 0.6                (or + 7 majeures + BTC + ETH)
  python backtest.py --days 180 --only S2,S3
  python backtest.py --days 120 --set strategies.S1_ADX_MIN=15,18,22 --set tracker.STRUCT_EXIT=true,false
  python backtest.py --source csv --file xauusd_m5.csv
  python backtest.py --source sim --days 60          (marche aléatoire : contrôle de bon sens)
Un CSV contient les colonnes epoch,open,high,low,close (bougies M5, epoch en secondes, ordre croissant).
Limites : spread constant (défaut par actif : or 0,015 %, majeures 0,006 %, BTC/ETH 0,05 %), pas de glissement, calendrier
économique non appliqué (rollover, vendredi soir et NFP le sont), un fichier CSV = un actif.
"""
import argparse, csv, itertools, math, random, sys, time
from collections import Counter
from datetime import datetime, timedelta

import gold
import guard as G
import strategies as S
import tracker as T

M5 = 300
SPREAD_DEFAULT = {"frxXAUUSD": 0.015, "cryBTCUSD": 0.05, "cryETHUSD": 0.05}   # % du prix (estimations)
SPREAD_OTHER = 0.006                          # majeures


# --------------------------------------------------------------------------
# Sources de données
# --------------------------------------------------------------------------
def load_csv(path):
    out = []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            try:
                e, o, h, l, c = (float(x) for x in row[:5])
            except ValueError:
                continue  # en-tête
            out.append({"epoch": int(e), "open": o, "high": h, "low": l, "close": c})
    out.sort(key=lambda x: x["epoch"])
    return out


def load_deriv(sym, days):
    """Télécharge ~days jours de M5 de l'actif en remontant par blocs de 5000 bougies."""
    api, out, end = gold.Deriv(), {}, "latest"
    target = int(days * 288)
    try:
        while len(out) < target:
            cs = api.candles(sym, M5, count=5000, end=end)
            if not cs:
                break
            new = {c["epoch"]: c for c in cs}
            if not set(new) - set(out):
                break
            out.update(new)
            end = min(new) - 1
            print(f"  {len(out)} bougies M5 téléchargées…")
    finally:
        api.close()
    return [out[k] for k in sorted(out)][-target:]


def gen_sim(days, seed, vol=0.0006, sub=10, start=1_700_000_000):
    """Marche aléatoire à volatilité constante (aucune structure de marché à exploiter)."""
    r, p, out = random.Random(seed), 2000.0, []
    start -= start % M5
    for i in range(int(days * 288)):
        o, hi, lo = p, p, p
        for _ in range(sub):
            p *= math.exp(r.gauss(0, vol / math.sqrt(sub)))
            hi, lo = max(hi, p), min(lo, p)
        out.append({"epoch": start + i * M5, "open": o, "high": hi, "low": lo, "close": p})
    return out


# --------------------------------------------------------------------------
# Rejeu : bougies de n'importe quel timeframe à l'instant t, sans look-ahead
# --------------------------------------------------------------------------
class Replay:
    def __init__(self, m5):
        self.m5, self.n, self._b = m5, len(m5), {}

    @staticmethod
    def _start(tf, epoch):
        g = S.GRAN[tf]
        if tf in ("D1", "H4"):                       # ouverture à 17h New York
            hours = 24 if tf == "D1" else 4
            dt = datetime.fromtimestamp(epoch, S.NY)
            h = (dt.hour - 17) % 24
            return int((dt.replace(minute=0, second=0, microsecond=0)
                        - timedelta(hours=h % hours)).timestamp())
        return epoch - epoch % g

    def _build(self, tf):
        buckets, bid, last = [], [], None
        for i, c in enumerate(self.m5):
            st = self._start(tf, c["epoch"])
            if st != last:
                buckets.append({"epoch": st, "open": c["open"], "high": c["high"],
                                "low": c["low"], "close": c["close"], "i0": i})
                last = st
            else:
                b = buckets[-1]
                b["high"], b["low"], b["close"] = max(b["high"], c["high"]), min(b["low"], c["low"]), c["close"]
            bid.append(len(buckets) - 1)
        self._b[tf] = (buckets, bid)

    def provide(self, tf, t, size=399):
        """Bougies du timeframe `tf` quand la bougie M5 d'index t vient de clôturer :
        `size` bougies clôturées + la bougie en formation (partielle ou vide)."""
        if tf not in self._b:
            self._build(tf)
        buckets, bid = self._b[tf]
        nxt = bid[t + 1] if t + 1 < self.n else bid[t]
        closed = buckets[max(0, nxt - size):nxt]
        if nxt == bid[t]:
            part = self.m5[buckets[nxt]["i0"]:t + 1]
            forming = {"epoch": buckets[nxt]["epoch"], "open": part[0]["open"],
                       "high": max(x["high"] for x in part), "low": min(x["low"] for x in part),
                       "close": part[-1]["close"]}
        else:
            px = self.m5[t]["close"]
            forming = {"epoch": buckets[nxt]["epoch"], "open": px, "high": px, "low": px, "close": px}
        return closed + [forming]


# --------------------------------------------------------------------------
# Exécution du backtest
# --------------------------------------------------------------------------
def run(m5, sym, spread_pct=0.0, warmup_days=25, progress=True, split=None):
    """warmup_days : l'EMA 200 du H4 demande ~35 jours de bourse ; en dessous, S1 reste muet au début."""
    rp, funnel, sent = Replay(m5), Counter(), {}
    tr = {"active": {}, "closed": []}
    warm = min(int(warmup_days * 288), len(m5) // 3)
    boundary = m5[warm + int((len(m5) - warm) * split)]["epoch"] if split else None
    stats = Counter()
    t_start = time.time()
    for t in range(warm, len(m5) - 1):
        now = m5[t]["epoch"] + M5
        # 1) fait avancer les trades avec la bougie M5 qui vient de clôturer
        for key, tk in list(tr["active"].items()):
            tk["gran"] = M5
            for e in T.advance(tk, [m5[t]], now):
                stats[e] += 1
            if tk["status"] not in T.ACTIVE:
                T.archive(tr, key)
        # 2) cherche de nouveaux setups
        provide = lambda tf, force=False, _t=t: rp.provide(tf, _t)
        for sig, _ in gold.scan(sym, provide, now, sent, funnel, ()):
            sent[sig["key"]] = now
            ok, why = G.risk_check(tr, sym, sig["dir"], now, sig["label"])
            if not ok:
                funnel["dedupe" if why == "dedupe" else "risk"] += 1
                continue
            funnel["alert"] += 1
            risk = abs(sig["entry"] - sig["sl"])
            extra = dict(sig["extra"], session=sig["session"],
                         cost_r=(spread_pct / 100 * sig["entry"]) / risk if risk else 0.0)
            tr["active"][sig["key"]] = T.new_trade(
                gold.BOT, sig["key"], sym, sig["name"], sig["dir"], sig["otype"], sig["entry"], sig["sl"],
                sig["tp1"], sig["tp2"], sig["tp3"], sig["ltf"], M5, sig["dec"], sig["label"], now,
                gold.TRADE_TTL, extra=extra, inval=sig["inval"], bar_secs=sig["gran"])
        if progress and (t - warm) % 5000 == 0:
            print(f"  {gold.name_of(sym)} · pas {t - warm}/{len(m5) - warm - 1} · {funnel['alert']} alertes · "
                  f"{time.time() - t_start:.0f}s", flush=True)
    for tk in tr["closed"]:
        tk["r"] = round(tk["r"] - tk.get("cost_r", 0.0), 3)      # coût du spread, en R
        if boundary:
            tk["period"] = "1" if tk["t0"] < boundary else "2"
    return tr, funnel, stats


def mean_ci(rs, n=2000, seed=1):
    """Intervalle de confiance à 95 % de la moyenne par rééchantillonnage (bootstrap)."""
    rnd, m = random.Random(seed), len(rs)
    means = sorted(sum(rnd.choice(rs) for _ in range(m)) / m for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def verdict(rs):
    if len(rs) < 30:
        return f"Trop peu de trades ({len(rs)}) pour conclure : il en faut au moins 30, idéalement plusieurs centaines."
    lo, hi = mean_ci(rs)
    head = f"Espérance par trade : IC 95 % [{lo:+.2f} ; {hi:+.2f}] R"
    if lo > 0:
        return head + " : positive avec une bonne probabilité (à confirmer sur d'autres périodes)."
    if hi < 0:
        return head + " : négative avec une bonne probabilité."
    return head + " : indistinguable de zéro, aucun avantage démontré."


def breakdown(ts, key):
    rows = []
    for val in sorted({str(t.get(key)) for t in ts if t.get(key) is not None}):
        s = T.summarize([t for t in ts if str(t.get(key)) == val])
        if s:
            rows.append(T.fmt_summary(f"  {key} {val}", s))
    return rows


def run_all(datasets, spread_pct, warmup_days, split, progress=True):
    """datasets : [(sym, m5)]. Retourne [(sym, tr, funnel, stats)]. spread_pct None : défaut par actif."""
    res = []
    for sym, m5 in datasets:
        sp = spread_pct if spread_pct is not None else SPREAD_DEFAULT.get(sym, SPREAD_OTHER)
        tr, funnel, stats = run(m5, sym, sp, warmup_days, progress, split)
        res.append((sym, tr, funnel, stats))
    return res


def report(results, days, spread_pct, split=None):
    """results : liste de (sym, tr, funnel, stats). Retourne les lignes du rapport."""
    ts = [t for (_, tr, _, _) in results for t in tr["closed"]]
    funnel, stats = Counter(), Counter()
    for _, _, f, st in results:
        funnel.update(f)
        stats.update(st)
    pending = sum(len(T.active_list(tr)) for (_, tr, _, _) in results)
    sp = f"{spread_pct:g} %" if spread_pct is not None else "par défaut (or 0,015 %, majeures 0,006 %, BTC/ETH 0,05 %)"
    L = [f"BACKTEST · {', '.join(gold.name_of(r[0]) for r in results)} · {days:g} jours · spread {sp} · "
         f"stratégies {', '.join(gold.ENABLED)}",
         "Entonnoir : " + G.funnel_text(funnel),
         f"{funnel['alert']} alertes (ordres au marché) ; {pending} trade(s) encore ouvert(s). "
         "L'entonnoir compte des passages de scan, pas des setups distincts."]
    s = T.summarize(ts)
    if not s:
        return L + ["Aucun trade clôturé : pas assez de signaux sur cette période."]
    L.append(T.fmt_summary("TOTAL", s))
    L.append(verdict([t["r"] for t in ts]))
    if len(results) > 1:
        L.append("Par actif :")
        for sym, tr, _, _ in results:
            sub = T.summarize(tr["closed"])
            L.append(T.fmt_summary(f"  {gold.name_of(sym)}", sub) if sub else f"  {gold.name_of(sym)} : aucun trade")
    L.append("Par stratégie :")
    L += breakdown(ts, "cascade")
    for key in ["dir", "session", "liq", "h4", "tend_d1", "tend_h4", "result"] + (["period"] if split else []):
        L += breakdown(ts, key)
    if split:
        p1, p2 = [t["r"] for t in ts if t.get("period") == "1"], [t["r"] for t in ts if t.get("period") == "2"]
        if p1 and p2:
            m1, m2 = sum(p1) / len(p1), sum(p2) / len(p2)
            stable = (m1 > 0) == (m2 > 0)
            L.append(f"Stabilité : période 1 {m1:+.2f} R/trade ({len(p1)} trades), période 2 {m2:+.2f} R/trade "
                     f"({len(p2)} trades) : {'même signe' if stable else 'SIGNES OPPOSÉS, résultat peu fiable'}.")
    L += ["", f"Lecture : sous {s['n']} trades, ±{100 * math.sqrt(0.25 / s['n']):.0f} points sur le taux de gain "
              "sont du bruit. Un avantage réel se voit sur plusieurs centaines de trades, plusieurs actifs et "
              "plusieurs périodes. Une stratégie qui ne produit que quelques dizaines de trades n'est pas évaluable."]
    return L


# --------------------------------------------------------------------------
# Grille de réglages
# --------------------------------------------------------------------------
MODULES = {"strategies": S, "tracker": T, "guard": G, "gold": gold}


def coerce(v):
    low = v.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "none":
        return None
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v.strip()


def parse_sets(items):
    out = []
    for it in items or []:
        key, _, vals = it.partition("=")
        mod, _, name = key.strip().partition(".")
        if mod not in MODULES or not hasattr(MODULES[mod], name) or not vals:
            sys.exit(f"--set invalide : {it} (format module.NOM=v1,v2 ; modules : {', '.join(MODULES)})")
        out.append((mod, name, [coerce(v) for v in vals.split(",")]))
    return out


def grid_report(datasets, sets, a):
    combos = list(itertools.product(*[vals for _, _, vals in sets]))
    if len(combos) > 30:
        sys.exit(f"Grille trop grande ({len(combos)} combinaisons, maximum 30).")
    rows = []
    for combo in combos:
        saved = [(MODULES[m], n, getattr(MODULES[m], n)) for m, n, _ in sets]
        for (m, n, _), v in zip(sets, combo):
            setattr(MODULES[m], n, v)
        try:
            res = run_all(datasets, a.spread_pct, a.warmup_days, a.split, progress=False)
        finally:
            for mod, n, old in saved:
                setattr(mod, n, old)
        closed = [t for (_, tr, _, _) in res for t in tr["closed"]]
        s = T.summarize(closed)
        label = " ".join(f"{m}.{n}={v}" for (m, n, _), v in zip(sets, combo))
        per = ""
        if a.split:
            ms = []
            for p in ("1", "2"):
                rs = [t["r"] for t in closed if t.get("period") == p]
                ms.append(f"{sum(rs) / len(rs):+.2f}" if rs else "n/a")
            per = f" | P1 {ms[0]} · P2 {ms[1]} R/trade"
        pf = "∞" if s and s["pf"] == float("inf") else (f"{s['pf']:.2f}" if s else "")
        rows.append(f"{label} : " + (f"{s['n']} trades · {s['win']:.0f} % · {s['total']:+.1f} R (moy {s['avg']:+.2f}) "
                                     f"· PF {pf} · DD {s['dd']:.1f} R{per}" if s else "aucun trade"))
        print("  " + rows[-1], flush=True)
    note = ["", "Choisis un réglage seulement si P1 et P2 sont tous deux positifs et si l'écart avec les voisins est",
            "faible. Un réglage excellent sur une période et mauvais sur l'autre est du sur-ajustement."] if a.split else \
           ["", "Ajoute --split 0.6 pour voir la tenue de chaque réglage sur deux périodes (anti sur-ajustement)."]
    return [f"GRILLE · {', '.join(gold.name_of(x) for x, _ in datasets)} · {len(combos)} combinaisons"] + rows + note


def send_telegram(lines):
    text, chunk = "\n".join(lines), ""
    for ln in text.split("\n"):
        if len(chunk) + len(ln) + 1 > 3800:
            gold.notify(chunk)
            chunk = ""
        chunk += ln + "\n"
    if chunk.strip():
        gold.notify(chunk)


def main():
    ap = argparse.ArgumentParser(description="Backtest du bot (or + majeures) sur historique M5")
    ap.add_argument("--sym", default=gold.GOLD, help="actifs séparés par des virgules, ou 'all' (or + 7 majeures + BTC + ETH)")
    ap.add_argument("--source", choices=["deriv", "csv", "sim"], default="deriv")
    ap.add_argument("--file", help="CSV M5 (source csv, un seul actif)")
    ap.add_argument("--days", type=float, default=120)
    ap.add_argument("--only", help="stratégies à rejouer, ex. S2,S3 (défaut : toutes)")
    ap.add_argument("--spread-pct", type=float, default=None,
                    help="spread en %% du prix (défaut : or 0,015, majeures 0,006, BTC/ETH 0,05)")
    ap.add_argument("--warmup-days", type=float, default=25)
    ap.add_argument("--split", type=float, default=None, help="fraction de la période pour la période 1 (ex. 0.6)")
    ap.add_argument("--set", action="append", help="grille : module.NOM=v1,v2 (répétable)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--vol", type=float, default=0.0006, help="volatilité M5 de la marche aléatoire (sim)")
    ap.add_argument("--telegram", action="store_true", help="envoie aussi le rapport sur Telegram")
    a = ap.parse_args()
    if a.split is not None and not 0.2 <= a.split <= 0.8:
        sys.exit("--split doit être compris entre 0.2 et 0.8")
    if a.only:
        gold.ENABLED = [x.strip().upper() for x in a.only.split(",") if x.strip()]
    syms = gold.ALL_SYMBOLS if a.sym.strip().lower() == "all" else \
        [x.strip() for x in a.sym.split(",") if x.strip()]
    datasets = []
    for k, sym in enumerate(syms):
        if a.source == "csv":
            if not a.file or len(syms) > 1:
                sys.exit("--source csv demande --file et un seul actif")
            m5 = load_csv(a.file)
        elif a.source == "sim":
            m5 = gen_sim(a.days + a.warmup_days, a.seed + k, a.vol)
        else:
            print(f"Téléchargement de {a.days:g} jours de M5 pour {sym}…")
            m5 = load_deriv(sym, a.days + a.warmup_days)
        if len(m5) < 2000:
            print(f"{sym} : historique insuffisant ({len(m5)} bougies M5), actif ignoré.")
            continue
        print(f"{sym} : {len(m5)} bougies M5 ({len(m5) / 288:.0f} jours)")
        datasets.append((sym, m5))
    if not datasets:
        sys.exit("Aucune donnée exploitable.")
    sets = parse_sets(a.set)
    if sets:
        lines = grid_report(datasets, sets, a)
    else:
        res = run_all(datasets, a.spread_pct, a.warmup_days, a.split)
        lines = report(res, min(len(m5) for _, m5 in datasets) / 288, a.spread_pct, a.split)
    print("\n" + "=" * 60 + "\n" + "\n".join(lines))
    if a.telegram:
        send_telegram(lines)


if __name__ == "__main__":
    main()
