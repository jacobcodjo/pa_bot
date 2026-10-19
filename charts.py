"""Graphique d'un setup pour les alertes Telegram (matplotlib, sans interface graphique).

Entrée : un dictionnaire décrivant le setup
  {"title": str, "dec": int, "candles": [{epoch, open, high, low, close}, ...],
   "lines": [{"y": float, "label": str, "kind": "entry|sl|tp|range|sweep|info"}],
   "zones": [{"lo": float, "hi": float, "label": str, "kind": "fvg|ob|fib|entry"}]}
Sortie : un fichier PNG. Toute erreur est remontée à l'appelant, qui retombe alors sur le message texte seul.
"""
from datetime import datetime, timezone

BG, GRID, TXT = "#131722", "#2a2e39", "#d1d4dc"
UP, DOWN = "#26a69a", "#ef5350"
LINE_STYLE = {          # couleur, style, épaisseur
    "entry": ("#4fc3f7", "-", 1.4), "sl": ("#ff5252", "-", 1.4), "tp": ("#66bb6a", "-", 1.2),
    "range": ("#9e9e9e", "--", 1.0), "sweep": ("#ffa726", ":", 1.4), "info": ("#b0bec5", ":", 1.0),
}
ZONE_STYLE = {"fvg": "#ab47bc", "ob": "#42a5f5", "fib": "#ffd54f", "entry": "#4fc3f7"}


def _fmt(y, dec):
    return f"{y:.{dec}f}" if dec is not None else f"{y:.5f}".rstrip("0").rstrip(".")


def render(chart, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    cs, dec = chart["candles"], chart.get("dec")
    n = len(cs)
    if n < 5:
        raise ValueError("pas assez de bougies pour un graphique")
    lines, zones = chart.get("lines", []), chart.get("zones", [])

    # échelle verticale : bougies + entrée, stop, TP1 et TP2 (TP3 et le reste seulement s'ils tiennent)
    view = cs[-40:]                              # l'échelle suit les 40 dernières bougies
    core = [c["low"] for c in view] + [c["high"] for c in view]
    for ln in lines:
        if ln["kind"] in ("entry", "sl") or ln["label"] in ("TP1", "TP2"):
            core.append(ln["y"])
    lo, hi = min(core), max(core)
    pad = (hi - lo) * 0.06 or hi * 0.001
    ylo, yhi = lo - pad, hi + pad

    fig, ax = plt.subplots(figsize=(6.4, 4.4), dpi=140)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    xmax = n + max(8, n * 0.28)                 # marge à droite pour les étiquettes

    zlabels = []
    for z in zones:
        col = ZONE_STYLE.get(z["kind"], "#90a4ae")
        zlo, zhi = max(z["lo"], ylo), min(z["hi"], yhi)
        if zhi <= zlo:
            continue
        ax.add_patch(Rectangle((-0.5, zlo), xmax, zhi - zlo, facecolor=col, alpha=0.16, edgecolor="none", zorder=1))
        zlabels.append(((zlo + zhi) / 2, z["label"], col))

    for i, c in enumerate(cs):
        col = UP if c["close"] >= c["open"] else DOWN
        ax.plot([i, i], [c["low"], c["high"]], color=col, linewidth=0.9, zorder=3)
        body_lo, body_hi = min(c["open"], c["close"]), max(c["open"], c["close"])
        ax.add_patch(Rectangle((i - 0.32, body_lo), 0.64, max(body_hi - body_lo, (yhi - ylo) * 0.002),
                               facecolor=col, edgecolor=col, zorder=4))

    labels = []                                  # (y réel, texte, couleur)
    for ln in lines:
        col, style, w = LINE_STYLE.get(ln["kind"], LINE_STYLE["info"])
        y, text = ln["y"], f"{ln['label']} {_fmt(ln['y'], dec)}"
        if ylo <= y <= yhi:
            ax.plot([-0.5, xmax], [y, y], color=col, linestyle=style, linewidth=w, zorder=5)
            labels.append((y, text, col))
        else:
            labels.append((yhi if y > yhi else ylo, f"{'▲' if y > yhi else '▼'} {text}", col))
    # on écarte les étiquettes qui se chevauchent (les lignes gardent leur vrai niveau)
    gap = (yhi - ylo) * 0.052
    labels.sort(key=lambda x: -x[0])
    pos = []
    for y, _, _ in labels:
        pos.append(y if not pos else min(y, pos[-1] - gap))
    if pos and pos[-1] < ylo + gap * 0.4:         # trop bas : on remonte tout le groupe
        shift = ylo + gap * 0.4 - pos[-1]
        pos = [p + shift for p in pos]
        for k in range(1, len(pos)):               # puis on réécarte vers le haut si nécessaire
            pos[k] = min(pos[k], pos[k - 1] - gap)
    for (y, text, col), py in zip(labels, pos):
        ax.text(xmax - 0.3, py, text, color=col, fontsize=7, va="center", ha="right", zorder=8,
                fontweight="bold", bbox=dict(facecolor=BG, edgecolor="none", pad=1.0, alpha=0.85))

    zlabels.sort(key=lambda x: -x[0])
    zpos = []
    for y, _, _ in zlabels:
        zpos.append(y if not zpos else min(y, zpos[-1] - gap))
    for (y, text, col), py in zip(zlabels, zpos):
        ax.text(0, py, f" {text}", color=col, fontsize=6.5, va="center", ha="left", zorder=8,
                bbox=dict(facecolor=BG, edgecolor="none", pad=0.8, alpha=0.7))

    # repère de l'heure de l'alerte : la dernière bougie
    ax.axvline(n - 1, color="#546e7a", linewidth=0.6, linestyle=":", zorder=2)

    step = max(1, n // 5)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([datetime.fromtimestamp(cs[i]["epoch"], timezone.utc).strftime("%d/%m %H:%M") for i in ticks],
                       color=TXT, fontsize=6.5)
    ax.tick_params(axis="y", colors=TXT, labelsize=6.5)
    ax.yaxis.tick_left()
    ax.set_xlim(-0.5, xmax)
    ax.set_ylim(ylo, yhi)
    ax.grid(color=GRID, linewidth=0.4)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.set_title(chart.get("title", ""), color=TXT, fontsize=9, loc="left", pad=6)
    ax.text(1, 1.012, "UTC", transform=ax.transAxes, color="#78909c", fontsize=6.5, ha="right", va="bottom")
    fig.tight_layout()
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    return path


def candles_slice(cs, n=70):
    """Dernières bougies (clôturées + en formation) réduites aux champs utiles."""
    return [{"epoch": c["epoch"], "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]}
            for c in cs[-n:]]
