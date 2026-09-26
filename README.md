# Bot Price Action (Forex / Or / Crypto)

Bot d'alertes Telegram avec **deux stratégies indépendantes**, qui tournent
en parallèle sur chaque symbole -- chaque alerte précise dans le message
laquelle des deux l'a déclenchée :

- **Impulse** : tendance de fond (HTF) -> jambe impulsive dans le sens de la
  tendance -> zone Fibonacci de correction 61.8%-79% -> cassure de structure
  (trigger). Règles identiques à l'ancien bot Impulse (`strategy.py`).
- **CRT** (Candle Range Trading) : range de référence (HTF) -> sweep de
  liquidité (manipulation) + Fair Value Gap / Order Block (MTF) -> cassure de
  structure (confirmation, LTF). Règles identiques à l'ancien bot CRT
  (`crt_strategy.py`).

## Sources de données -- pourquoi pas OANDA ni websocket Deriv

1. **Forex + Or : Twelve Data**, pas OANDA. OANDA est un *broker* : il exige
   un compte de trading (même "practice") et n'accepte pas les clients de
   nombreux pays (dont la plupart des pays d'Afrique de l'Ouest -- ce n'est
   pas un problème temporaire, leur propre sélecteur de pays ne les liste
   pas). Twelve Data n'est qu'un **fournisseur de données de marché** :
   inscription par simple email sur twelvedata.com, sans compte de trading
   ni vérification d'identité, utilisable depuis n'importe où.
2. **Crypto : Binance** (API publique, sans clé).
3. **Plus de websocket.** Chaque bougie est récupérée par une simple requête
   HTTP -- pas de connexion persistante à travers Cloudflare (source des
   rejets rencontrés avec l'ancien bot Deriv), et pas besoin d'en garder une
   ouverte puisque le bot tourne par cron toutes les 15 min.

### Limite du palier gratuit Twelve Data et cache

Le palier gratuit Twelve Data est limité à **800 requêtes/jour (8/minute)**.
Pour rester dans cette limite, `data_client.py` garde un **cache disque**
(`candle_cache.json`) : une bougie n'est re-téléchargée que lorsque sa
période est révolue (une H4 n'est refetchée qu'après 4h, une D1 qu'après
24h, etc.) -- seule la M15 est vraiment retéléchargée à chaque passage.
Si le quota venait quand même à être dépassé un jour très chargé, le bot
retombe automatiquement sur le cache existant (même expiré) plutôt que
d'échouer. Si besoin de plus de marge, le palier payant Twelve Data (à
partir de ~12 $/mois) lève cette limite.

## Filtre killzones, avec fuseau horaire automatique

Le bot ne scanne que les symboles pertinents pour la session actuellement
active (voir `sessions.py` / `config.py`) :

| Killzone   | Fenêtre (heure locale)            | Symboles                        |
|------------|-------------------------------------|----------------------------------|
| Asiatique  | 09h00 - 13h00 (Asia/Tokyo)           | Paires JPY, AUD, NZD            |
| Londres    | 08h00 - 11h00 (Europe/London)        | EUR, GBP, CHF, or, BTC/ETH      |
| New York   | 08h00 - 11h00 (America/New_York)     | Paires USD majeures, or, crypto |

Chaque fenêtre est définie dans le fuseau **local** de sa propre place
financière (via le module `zoneinfo` de Python), pas en UTC+1 fixe : le
changement d'heure (DST) de Londres et New York est donc géré
**automatiquement**, sans aucun réglage à refaire quand l'Europe ou les
Etats-Unis basculent heure d'été/hiver (à des dates différentes l'une de
l'autre). Le Japon n'observe pas le changement d'heure, sa fenêtre reste
stable toute l'année.

Les messages Telegram affichent l'heure en WAT (UTC+1, Afrique de l'Ouest,
fixe toute l'année) via `DISPLAY_TZ` dans `config.py`.

## Installation

```
pip install -r requirements.txt
```

## Variables d'environnement / secrets nécessaires

| Variable                 | Description                                              |
|---------------------------|-----------------------------------------------------------|
| `TWELVEDATA_API_KEY`       | Clé API Twelve Data (compte gratuit)                       |
| `PA_TELEGRAM_BOT_TOKEN`    | Token du bot Telegram                                      |
| `PA_TELEGRAM_CHAT_ID`      | ID du chat/canal Telegram cible                            |

Pour obtenir une clé Twelve Data : créer un compte gratuit sur
twelvedata.com (email + mot de passe, aucune vérification d'identité), puis
copier la clé API affichée sur le tableau de bord.

Sur GitHub : les trois secrets vont dans **Settings -> Secrets and
variables -> Actions -> Secrets**.

## Déclenchement fiable via cron-job.org (recommandé)

Le cron natif de GitHub Actions (`schedule`) est gardé en filet de sécurité,
mais il est connu pour être peu fiable -- retards fréquents, surtout aux
heures de forte charge sur GitHub. Comme sur l'ancien bot, il vaut mieux
déclencher le scan depuis un service de cron externe.

1. Crée un token GitHub : **Settings (du compte, pas du dépôt) -> Developer
   settings -> Personal access tokens -> Fine-grained tokens** -> génère-en
   un avec accès en écriture (`Contents` + `Actions`) sur ce dépôt
   uniquement.
2. Sur cron-job.org, crée une tâche toutes les 15 minutes qui envoie une
   requête **POST** vers :
   ```
   https://api.github.com/repos/<utilisateur>/<depot>/dispatches
   ```
   avec ces en-têtes :
   ```
   Authorization: Bearer <TON_TOKEN_GITHUB>
   Accept: application/vnd.github+json
   Content-Type: application/json
   ```
   et ce corps de requête :
   ```json
   {"event_type": "external-cron"}
   ```

Ça déclenche l'événement `repository_dispatch` du workflow, qui lance le
scan immédiatement -- exactement le même principe que sur l'ancien bot.

## Lancer un scan manuellement

```
python main.py
```

Si aucune killzone n'est active au moment de l'exécution, le script ne fait
aucun appel API et se termine immédiatement.

## Structure

- `config.py` -- symboles, killzones, paramètres des deux stratégies
- `sessions.py` -- détection de la killzone active (fuseau auto) et des symboles associés
- `twelvedata_client.py` / `binance_client.py` -- récupération des bougies
- `data_client.py` -- routage Twelve Data/Binance selon le symbole + cache disque
- `common.py` -- primitives partagées (swings, tendance, gaps de weekend)
- `strategy.py` -- stratégie Impulse (tendance + jambe impulsive + Fibonacci)
- `crt_strategy.py` -- stratégie CRT (range + sweep + FVG/Order Block)
- `state_manager.py` -- anti-doublon des alertes déjà envoyées (par stratégie)
- `trade_tracker.py` -- suivi des trades (remplissage, TP/SL, statistiques par stratégie et classe d'actif)
- `notifier.py` -- formatage (avec nom de la stratégie) et envoi des messages Telegram
- `main.py` -- point d'entrée, lance les deux stratégies sur chaque symbole actif

Cette logique est une implémentation simplifiée -- à backtester/affiner
avant tout usage réel avec de l'argent.
