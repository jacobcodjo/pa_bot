"""Tendance et âge de la tendance, pour le récapitulatif journalier (aucune alerte de trade n'en dépend).

Règles (bougies clôturées, fractales de 2 bougies de chaque côté), tendance HAUSSIÈRE :
  1. le dernier sommet ET le dernier creux sont plus hauts que les précédents (2 swings suffisent) ;
  2. structure intacte : aucune clôture sous le dernier creux depuis sa formation ;
  3. cassure par une clôture (pas une simple mèche) du sommet précédent (BOS_BY_CLOSE) ;
  4. âge = nombre de jambes consécutives (sommet + creux plus hauts) : 1 -> naissante, 2-3 -> établie,
     4 et plus -> mûre (proche de sa fin).
Tendance BAISSIÈRE : l'inverse. Sinon : pas de tendance claire.
Informations ajoutées : durée depuis le début de la série de jambes, niveau d'invalidation (dernier creux
pour une hausse, dernier sommet pour une baisse), et cassure « impulsive » (corps >= 1,2 x le corps moyen).
Inspiré de la lecture de tendance de l'ancien bot (jambes consécutives), sans reprendre ses stratégies.
"""
import strategies as S

SWING_N = 2
AGE_ESTABLISHED = 2       # jambes consécutives : 1 = naissante
AGE_MATURE = 4            # 4 jambes ou plus = mûre
BOS_BY_CLOSE = True
MIN_CANDLES = 12
TIMEFRAMES = ("D1", "H4", "H1")


def _run(vals):
    """Nombre de hausses successives à la fin de la liste (ex : 1,2,3 -> 2)."""
    n = 0
    for a, b in zip(vals[-2::-1], vals[:0:-1]):
        if b > a:
            n += 1
        else:
            break
    return n


def flip(cs):
    return [{"epoch": c["epoch"], "open": -c["open"], "high": -c["low"], "low": -c["high"], "close": -c["close"]}
            for c in cs]


def _up(h):
    """Tendance haussière sur des bougies clôturées, ou None."""
    if len(h) < MIN_CANDLES:
        return None
    sh, sl = S.swings(h, "high", SWING_N), S.swings(h, "low", SWING_N)
    if len(sh) < 2 or len(sl) < 2:
        return None
    highs, lows = [h[i]["high"] for i in sh], [h[i]["low"] for i in sl]
    if not (highs[-1] > highs[-2] and lows[-1] > lows[-2]):
        return None
    if any(c["close"] < lows[-1] for c in h[sl[-1] + 1:]):
        return None                                   # structure cassée
    brk = next((i for i in range(sh[-2] + 1, len(h)) if h[i]["close"] > highs[-2]), None)
    if BOS_BY_CLOSE and brk is None:
        return None                                   # sommet dépassé seulement par une mèche
    legs = max(1, min(_run(highs), _run(lows)))
    age = "naissante" if legs < AGE_ESTABLISHED else "établie" if legs < AGE_MATURE else "mûre"
    start = min(sh[-(legs + 1)], sl[-(legs + 1)])
    impulsive = False
    if brk is not None and brk >= 1:
        avg = S.body_avg(h[max(0, brk - 20):brk])
        impulsive = avg > 0 and S.body(h[brk]) >= S.DISP_MULT * avg
    return {"legs": legs, "age": age, "start_epoch": h[start]["epoch"], "invalid": lows[-1],
            "impulsive": impulsive}


def trend_info(candles):
    """candles : bougies du timeframe, la dernière est en formation (ignorée).
    Retourne {"dir": "BUY"|"SELL", "age", "legs", "start_epoch", "invalid", "impulsive"} ou None."""
    h = candles[:-1]
    up = _up(h)
    if up:
        return dict(up, dir="BUY")
    dn = _up(flip(h))
    if dn:
        return dict(dn, dir="SELL", invalid=-dn["invalid"])
    return None


def fmt_duration(sec):
    sec = max(0, sec)
    return f"{sec / 86400:.0f} j" if sec >= 2 * 86400 else f"{max(1, round(sec / 3600))} h"


def tf_line(tf, info, now, dec):
    if info is None:
        return f"  {tf}  ⚪ Pas de tendance claire"
    buy = info["dir"] == "BUY"
    legs = f"{info['legs']} jambe" + ("s" if info["legs"] > 1 else "")
    return (f"  {tf}  {'🟢 Haussière' if buy else '🔴 Baissière'} · {info['age']} ({legs}, "
            f"{fmt_duration(now - info['start_epoch'])}){' ⚡' if info['impulsive'] else ''} · "
            f"invalidation {info['invalid']:.{dec}f}")


def summary(infos):
    """Phrase de synthèse sur l'alignement D1 / H4 / H1. infos : {tf: info ou None}."""
    dirs = {tf: (infos.get(tf) or {}).get("dir") for tf in TIMEFRAMES}
    word = {"BUY": "haussiers", "SELL": "baissiers"}
    if dirs["D1"] and dirs["D1"] == dirs["H4"] == dirs["H1"]:
        return f"→ D1, H4 et H1 alignés ({word[dirs['D1']]})"
    if dirs["D1"] and dirs["D1"] == dirs["H4"]:
        rest = "sans tendance claire" if not dirs["H1"] else f"à contre-sens ({word[dirs['H1']]})"
        return f"→ D1 et H4 alignés ({word[dirs['D1']]}), H1 {rest}"
    if dirs["H4"] and dirs["H4"] == dirs["H1"]:
        rest = "sans tendance claire" if not dirs["D1"] else f"à contre-sens ({word[dirs['D1']]})"
        return f"→ H4 et H1 alignés ({word[dirs['H4']]}), D1 {rest}"
    return "→ timeframes non alignés"


ALERT_TFS = ("D1", "H4")


def alert_lines(d, infos, now):
    """Lignes de tendance (D1 et H4) ajoutées à une alerte de trade de sens d ('BUY'/'SELL'), et champs
    d'enregistrement pour le backtest. ✅ = tendance dans le sens du trade, ⚠️ = à contre-sens.
    C'est une information : aucune alerte n'est filtrée sur la tendance ou son âge."""
    lines, extra = [], {}
    for tf in ALERT_TFS:
        info = infos.get(tf)
        if info is None:
            lines.append(f"⚪ Tendance {tf} : pas de tendance claire")
            extra[f"tend_{tf.lower()}"] = "aucune"
            continue
        ok = info["dir"] == d
        legs = f"{info['legs']} jambe" + ("s" if info["legs"] > 1 else "")
        lines.append(f"{'✅' if ok else '⚠️'} Tendance {tf} {'haussière' if info['dir'] == 'BUY' else 'baissière'}"
                     f" · {info['age']} ({legs}, {fmt_duration(now - info['start_epoch'])})"
                     f"{'' if ok else ' : contre le trade'}")
        extra[f"tend_{tf.lower()}"] = f"{info['age']} {'avec' if ok else 'contre'}"
    return lines, extra


def symbol_block(name, infos, now, dec):
    return "\n".join([name] + [tf_line(tf, infos.get(tf), now, dec) for tf in TIMEFRAMES] + [f"  {summary(infos)}"])
