from config import CANDLE_COUNT
from sessions import get_active_sessions, get_active_symbols, is_forex_market_open
from data_client import get_many_candles, is_forex_or_gold
import strategy as impulse_strategy
import crt_strategy
import weekly_outlook
from notifier import send_telegram_message, format_setup_message
from state_manager import load_state, save_state, is_new_setup, mark_setup_sent, prune_state
from trade_tracker import (
    load_stats, save_stats, add_pending, resolve_pending, summarize,
    expire_stale_pending, prune_history,
)

ALL_TIMEFRAMES = ["D1", "H4", "H1", "M15"]
# M5 réservé à la crypto (Kraken) -- voir config.TIMEFRAME_CASCADE_CRT_CRYPTO_EXTRA.
CRYPTO_ONLY_TIMEFRAMES = ["M5"]


def build_specs(symbols):
    specs = [(symbol, tf, CANDLE_COUNT) for symbol in symbols for tf in ALL_TIMEFRAMES]
    specs += [
        (symbol, tf, CANDLE_COUNT)
        for symbol in symbols if not is_forex_or_gold(symbol)
        for tf in CRYPTO_ONLY_TIMEFRAMES
    ]
    return specs


def run():
    active_sessions = get_active_sessions()
    forex_open = is_forex_market_open()

    if weekly_outlook.should_send():
        try:
            weekly_outlook.build_and_send()
        except Exception as e:
            print(f"Echec de l'outlook hebdomadaire : {e}")

    if weekly_outlook.should_send_crypto_refresh():
        try:
            weekly_outlook.build_and_send_crypto_refresh()
        except Exception as e:
            print(f"Echec du rafraîchissement crypto du dimanche : {e}")

    active_symbols = get_active_symbols()  # {symbol: [sessions]} -- inclut la crypto 24/7 + forex/or si killzone/marché ouverts
    symbols = list(active_symbols.keys())

    if active_sessions:
        print(
            f"Killzone(s) forex active(s) : {', '.join(active_sessions)} -- {len(symbols)} symbole(s) à scanner"
            + ("" if forex_open else " (forex/or fermé -- week-end, crypto uniquement).")
        )
    else:
        print(f"Aucune killzone forex active à cette heure -- {len(symbols)} symbole(s) à scanner (crypto 24/7).")

    if not symbols:
        print("Aucun symbole à scanner ce passage.")
        return

    state = load_state()
    stats = load_stats()
    any_new = False

    specs = build_specs(symbols)

    # Les trades en attente peuvent concerner des symboles hors killzone
    # active à cet instant (ex: un setup asiatique encore en attente pendant
    # la session de Londres) -- on ajoute leur seul trigger_tf pour pouvoir
    # les résoudre à chaque passage, sans lancer une analyse complète dessus.
    # Les trades forex/or en attente sont ignorés tant que le marché est
    # fermé (rien ne peut de toute façon avoir bougé) pour ne pas gaspiller
    # de quota API le week-end.
    existing = {(s, tf) for s, tf, _ in specs}
    for trade in stats["pending"].values():
        if not forex_open and is_forex_or_gold(trade["symbol"]):
            continue
        key = (trade["symbol"], trade["trigger_tf"])
        if key not in existing:
            specs.append((trade["symbol"], trade["trigger_tf"], CANDLE_COUNT))
            existing.add(key)

    results = get_many_candles(specs)

    def candles_lookup(symbol, trigger_tf):
        candles = results.get((symbol, trigger_tf))
        return candles if candles and not isinstance(candles, Exception) else None

    resolved = resolve_pending(stats, candles_lookup)
    if resolved:
        print(f"{len(resolved)} trade(s) résolu(s) ce passage.")

    pruned_state_count = prune_state(state)
    expired_count = expire_stale_pending(stats)
    pruned_history_count = prune_history(stats)
    if pruned_state_count:
        print(f"{pruned_state_count} entrée(s) de state.json purgée(s) (trop anciennes).")
    if expired_count:
        print(f"{expired_count} trade(s) en attente expiré(s) (jamais rempli).")
    if pruned_history_count:
        print(f"{pruned_history_count} entrée(s) d'historique purgée(s) (trop anciennes).")

    for symbol in symbols:
        candles_by_tf = {}
        skip_symbol = False

        for tf in ALL_TIMEFRAMES:
            candles = results.get((symbol, tf))
            if isinstance(candles, Exception) or not candles:
                print(f"[{symbol}] Erreur de récupération ({tf}) : {candles}")
                skip_symbol = True
                break
            candles_by_tf[tf] = candles

        if skip_symbol:
            continue

        # M5 (crypto uniquement) : optionnel, n'empêche pas le reste de
        # l'analyse en cas d'échec -- seule la cascade H1->M15->M5 en dépend.
        if not is_forex_or_gold(symbol):
            m5_candles = results.get((symbol, "M5"))
            if isinstance(m5_candles, Exception) or not m5_candles:
                print(f"[{symbol}] Erreur de récupération (M5) : {m5_candles}")
            else:
                candles_by_tf["M5"] = m5_candles

        setups = impulse_strategy.analyze_symbol(symbol, candles_by_tf) + crt_strategy.analyze_symbol(symbol, candles_by_tf)

        for setup in setups:
            if is_new_setup(state, setup):
                message = format_setup_message(setup, active_symbols.get(symbol, []))
                try:
                    send_telegram_message(message)
                    print(
                        f"[{symbol}] Alerte envoyée : {setup['strategy']} {setup['direction']} "
                        f"({setup['htf']}→{setup['zone_tf']}→{setup['trigger_tf']})"
                    )
                except Exception as e:
                    print(f"[{symbol}] Échec d'envoi Telegram : {e}")
                    continue
                mark_setup_sent(state, setup)
                add_pending(stats, setup)
                any_new = True

    if not any_new:
        print("Aucun nouveau setup détecté sur ce passage.")

    save_state(state)
    save_stats(stats)

    print(summarize(stats))


if __name__ == "__main__":
    run()
