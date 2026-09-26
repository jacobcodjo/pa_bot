"""
Détermine la ou les killzone(s) actives à l'instant présent.

Chaque killzone est définie en heure LOCALE de sa propre place financière
(voir SESSION_WINDOWS dans config.py). On convertit l'heure UTC actuelle vers
le fuseau local de chaque session via `zoneinfo`, qui gère lui-même le
changement d'heure (DST) de cette région -- la fenêtre reste donc toujours
correcte, sans aucun réglage manuel à refaire quand l'Europe ou les Etats-Unis
changent d'heure.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config import SESSION_WINDOWS, SESSION_SYMBOLS, DISPLAY_TZ


def _in_window(local_dt: datetime, window: dict) -> bool:
    start_h, start_m = window["start"]
    end_h, end_m = window["end"]
    now_minutes = local_dt.hour * 60 + local_dt.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    return start_minutes <= now_minutes < end_minutes


def get_active_sessions(now_utc: datetime = None) -> list:
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    active = []
    for name, window in SESSION_WINDOWS.items():
        local_dt = now_utc.astimezone(ZoneInfo(window["tz"]))
        if _in_window(local_dt, window):
            active.append(name)
    return active


def get_active_symbols(now_utc: datetime = None) -> dict:
    """Retourne {symbol: [sessions actives qui le concernent]} pour l'instant présent."""
    active = get_active_sessions(now_utc)
    symbols = {}
    for session in active:
        for symbol in SESSION_SYMBOLS.get(session, []):
            symbols.setdefault(symbol, []).append(session)
    return symbols


def display_time(now_utc: datetime = None) -> str:
    """Heure actuelle formatée dans le fuseau d'affichage (UTC+1, WAT)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(ZoneInfo(DISPLAY_TZ))
    offset = local.utcoffset()
    offset_hours = int(offset.total_seconds() // 3600) if offset else 0
    return f"{local.strftime('%H:%M')} (UTC{offset_hours:+d})"
