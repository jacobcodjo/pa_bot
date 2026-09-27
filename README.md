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
2. **Crypto : Kraken** (API publique, sans clé). Pas Binance : l'API publique
   de Binance renvoie une erreur 451 depuis les adresses IP des datacenters
   cloud américains (Azure/AWS) -- exactement l'infrastructure des runners
   GitHub Actions. Ce n'est pas un problème de quota, Binance bloque la
   connexion à la source ; aucune retentative n'y change rien. Kraken
   n'applique pas ce type de restriction.
3. **Plus de websocket.** Chaque bougie est récupérée par une simple requête
   HTTP -- pas de connexion persistante à travers Cloudflare (source des
   rejets rencontrés avec l'ancien bot Deriv), et pas besoin d'en garder une
   ouverte puisque le bot tourne par cron toutes les 5 min.

### Fréquence du scan (5 minutes)

Le bot tourne toutes les 5 minutes. Ça n'augmente pas la charge sur Twelve
Data : le cache est basé sur la durée réelle de chaque bougie (une M15 reste
valide 15 minutes, peu importe combien de fois le script tourne entre-temps),
donc le nombre réel d'appels API par jour reste quasiment identique à un
scan moins fréquent -- seul le délai de détection d'un nouveau setup est
réduit (jusqu'à 5 min au lieu de 15). Le facteur qui aurait pu limiter cette
fréquence est le quota de minutes GitHub Actions (2000 min/mois gratuites
sur un dépôt privé) -- sur un **dépôt public**, ces minutes sont illimitées
et gratuites, donc aucune contrainte de ce côté.

### Outlook hebdomadaire (week-end)

Pendant la fermeture du forex (voir ci-dessus), le bot envoie **une seule
fois par week-end** un message récapitulatif du biais directionnel de fond
pour chaque actif -- forex/or (dernière clôture connue avant la fermeture)
et crypto (qui continue de trader normalement) :

```
📅 Outlook de la semaine — 09:00 (UTC+1)

Forex
EUR/USD : 🟢 Haussier (D1) / 🟢 Haussier (W1)
GBP/USD : 🔴 Baissier (D1) / ⚪ Range (W1)
...

Or
XAU/USD : 🟢 Haussier (D1) / 🟢 Haussier (W1)

Crypto
BTCUSDT : 🟢 Haussier (D1) / ⚪ Range (W1)
...
```

Ce n'est pas une troisième stratégie de trading : c'est une lecture de la
tendance de fond (même logique que celle utilisée par Impulse et CRT en
interne), sans setup d'entrée/sortie associé -- juste un repère pour préparer
la semaine à venir. Envoyé dès la fermeture du vendredi soir, avec un
marqueur (`weekly_outlook_state.json`) qui empêche tout renvoi le reste du
week-end.

**Rafraîchissement crypto du dimanche.** Contrairement au forex qui reste
figé sur la même clôture jusqu'à lundi, la crypto continue de trader tout le
week-end -- la lecture du vendredi soir devient donc de moins en moins
pertinente pour elle au fil du week-end. Un second message, crypto
uniquement, est donc envoyé le dimanche à partir de 20h00 UTC (avant la
réouverture du forex ~22h00 UTC), avec son propre marqueur pour ne pas se
répéter :

```
🔄 Mise à jour crypto (dimanche) — 21:00 (UTC+1)

BTCUSDT : 🟢 Haussier (D1) / ⚪ Range (W1)
...
```

### Fermeture du week-end (forex/or)

Le forex et l'or ferment du vendredi ~22h00 UTC au dimanche ~22h00 UTC
(`sessions.is_forex_market_open`). Pendant cette période, ces symboles sont
automatiquement exclus du scan -- même si une fenêtre de killzone tombe
techniquement sur un samedi ou dimanche (les killzones sont définies par
heure de la journée, sans notion de jour de la semaine). La crypto continue
de tourner normalement, 24/7. Ça évite de gaspiller du quota Twelve Data à
re-télécharger des bougies figées sur un marché fermé.

### Limite du palier gratuit Twelve Data et cache

Le palier gratuit Twelve Data est limité à **800 requêtes/jour ET 8/minute**.
Cette seconde limite est stricte : au moindre dépassement, l'API répond par
une erreur 429 ("Too Many Requests"). `twelvedata_client.py` espace donc
chaque requête d'environ 8 secondes en interne, quel que soit le nombre de
symboles à scanner -- un premier scan à froid (cache vide, ~14 paires forex x
4 timeframes) peut ainsi prendre plusieurs minutes, c'est normal et attendu.
En plus de ça, `data_client.py` garde un **cache disque**
(`candle_cache.json`) : une bougie n'est re-téléchargée que lorsque sa
période est révolue (une H4 n'est refetchée qu'après 4h, une D1 qu'après
24h, etc.) -- seule la M15 est vraiment retéléchargée à chaque passage, ce
qui rend les scans suivants bien plus rapides. Si le quota venait quand même
à être dépassé un jour très chargé, le bot retombe automatiquement sur le
cache existant (même expiré) plutôt que d'échouer. Si besoin de plus de
marge, le palier payant Twelve Data (à partir de ~66 $/mois) lève la limite
par minute.

## Filtre killzones (forex/or), avec fuseau horaire automatique

Le forex/or n'est scanné que pendant la session actuellement active (voir
`sessions.py` / `config.py`) :

| Killzone   | Fenêtre (heure locale)            | Symboles                        |
|------------|-------------------------------------|----------------------------------|
| Asiatique  | 09h00 - 13h00 (Asia/Tokyo)           | Paires JPY, AUD, NZD            |
| Londres    | 08h00 - 11h00 (Europe/London)        | EUR, GBP, CHF, or                |
| New York   | 08h00 - 11h00 (America/New_York)     | Paires USD majeures, or          |

**La crypto (BTC, ETH, LTC, XRP) n'est pas soumise aux killzones** : elle
trade 24/7, elle est donc scannée à chaque passage, quelle que soit l'heure
(`ALWAYS_ON_SYMBOLS` dans `config.py`) -- les alertes crypto affichent "24/7"
au lieu du nom d'une session.

Chaque fenêtre killzone est définie dans le fuseau **local** de sa propre
place financière (via le module `zoneinfo` de Python), pas en UTC+1 fixe : le
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
2. Sur cron-job.org, crée une tâche toutes les 5 minutes qui envoie une
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
- `twelvedata_client.py` / `kraken_client.py` -- récupération des bougies
- `data_client.py` -- routage Twelve Data/Kraken selon le symbole + cache disque
- `common.py` -- primitives partagées (swings, tendance, gaps de weekend)
- `strategy.py` -- stratégie Impulse (tendance + jambe impulsive + Fibonacci)
- `crt_strategy.py` -- stratégie CRT (range + sweep + FVG/Order Block)
- `state_manager.py` -- anti-doublon des alertes déjà envoyées (par stratégie)
- `trade_tracker.py` -- suivi des trades (remplissage, TP/SL, statistiques par stratégie et classe d'actif)
- `notifier.py` -- formatage (avec nom de la stratégie) et envoi des messages Telegram
- `weekly_outlook.py` -- outlook hebdomadaire (biais D1/W1), envoyé une fois par week-end
- `main.py` -- point d'entrée, lance les deux stratégies sur chaque symbole actif

Cette logique est une implémentation simplifiée -- à backtester/affiner
avant tout usage réel avec de l'argent.
