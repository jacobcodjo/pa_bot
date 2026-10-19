# Bot or (XAUUSD)

Bot d'alertes Telegram dédié à l'or, avec quatre stratégies propres à ce marché (détail précis dans
[STRATEGIES.md](STRATEGIES.md)). Il reprend l'architecture du bot CRT/Impulsion (client Deriv robuste, suivi
des trades, garde-fous, graphique et boutons Telegram, backtest) mais **aucune de ses stratégies**.

| Code | Stratégie | Unités de temps |
|---|---|---|
| S1 | Tendance + repli | H4 > H1 > M15 |
| S2 | Cassure de la plage asiatique | M15 |
| S3 | Prise de liquidité + CHoCH | M15 > M5 |
| S4 | Retour à la moyenne en range | H1 > M15 |

Le bot **n'exécute aucun ordre** : il envoie des alertes (entrée, stop, TP1 à TP3 avec ratio, lot suggéré,
graphique) et suit leur résultat en R. Aucune stratégie n'est garantie ; les paramètres sont des points de
départ à valider par backtest.

## Fichiers

| Fichier | Rôle |
|---|---|
| `gold.py` | Scan, client Deriv (limites de requêtes, reconnexion), alertes et commandes Telegram, suivi |
| `strategies.py` | Les 4 stratégies, indicateurs (EMA, ATR, ADX, RSI, Bollinger, swings), sessions |
| `tracker.py` | Suivi des trades : TP1/TP2/TP3, stop à l'entrée, sorties de structure et au temps, statistiques |
| `guard.py` | Fenêtres sans alerte (news USD, rollover, week-end), limites de risque, lot, santé du bot, bilan |
| `charts.py` | Graphique joint à l'alerte |
| `backtest.py` | Rejeu M5 des 4 stratégies, par stratégie, avec deux périodes et grille de réglages |
| `STRATEGIES.md` | Règles précises de chaque stratégie |
| `.github/workflows/gold.yml` | Scan (déclenché par un cron externe) |
| `.github/workflows/backtest.yml` | Backtest manuel |

## Installation

1. Crée un dépôt GitHub avec le contenu de ce dossier (le dossier `.github/workflows` inclus).
2. **Settings > Secrets and variables > Actions** : secrets `TELEGRAM_TOKEN` et `TELEGRAM_CHAT_ID`.
3. Variables facultatives (onglet Variables) :
   - `ACCOUNT_BALANCE` et `RISK_PCT` (ex. 10000 et 1) : affichent le lot suggéré (100 oz par lot ; vérifie sur ta plateforme).
   - `STRATEGIES` : liste à activer, ex. `S2,S3` (défaut : `S1,S2,S3,S4`).
4. Le workflow n'a pas de `schedule` : déclenche-le toutes les 5 minutes avec ton cron externe
   (`workflow_dispatch`), comme pour l'ancien bot.
5. Lance une fois le workflow avec `test = true` : tu reçois un message de contrôle avec l'entonnoir.

Les fichiers d'état (`cache.json`, `state.json`, `trades.json`, `guard.json`) sont conservés par le cache GitHub.
Pour les jours d'annonces non couverts par le calendrier automatique, copie `news.example.json` en `news.json`.

## Commandes Telegram

Boutons **Pris / Ignoré** sous chaque alerte, et : `/pris N`, `/ignore N`, `/r N X` (ton résultat réel du
trade #N en R, ex. `/r 12 -0.8`), `/bilan` (30 derniers jours, par stratégie). Les commandes sont lues à chaque
scan (réponse sous 5 minutes).

## Backtest

Workflow **Backtest Or** (ou en local : `python backtest.py --days 120 --split 0.6`).

- Rejoue les mêmes fonctions que le bot en direct, sans regarder le futur, avec un spread (défaut 0,015 %).
- Rapport par stratégie, sens, session, alignement H4, résultat, et stabilité entre deux périodes.
- `--only S2,S3` pour une stratégie ; `--set strategies.S1_ADX_MIN=15,18,22` pour une grille ; `--source csv`
  pour tes propres données ; `--source sim` pour un contrôle de bon sens sur marche aléatoire.
- Il faut environ 25 jours d'échauffement (EMA 200 du H4) : demande `--days` en plus.

**Ce que le backtest peut et ne peut pas dire** : sous quelques centaines de trades, un taux de gain n'est que
du bruit. Retiens une stratégie seulement si l'espérance est positive sur les deux périodes, avec un intervalle
de confiance qui exclut zéro, et après avoir ajouté ton vrai spread. Une stratégie à quelques dizaines de
trades n'est pas évaluable.

## Ce qui a été vérifié, et ce qui ne l'a pas été

Vérifié (données simulées, sans accès réseau) : le code s'exécute de bout en bout, les 4 stratégies produisent des
signaux, le suivi (TP1/TP2/TP3, stop, doublons), le message, le lot et le graphique fonctionnent, et sur une marche
aléatoire l'espérance est proche de zéro (pas de gain « gratuit » dans le code).

**Non vérifié** : la connexion à l'API Deriv et l'envoi Telegram (pas d'accès réseau pendant la rédaction), et
surtout **la rentabilité sur l'or réel**. Fais d'abord un premier run avec `test = true`, puis un backtest sur
Deriv, avant de suivre les alertes.
