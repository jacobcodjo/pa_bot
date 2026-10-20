# Bot or (XAUUSD) + paires majeures + BTC/ETH

Bot d'alertes Telegram pour l'or, les 7 paires forex majeures, le BTC et l'ETH, avec quatre stratégies (détail précis dans
[STRATEGIES.md](STRATEGIES.md)). Il reprend l'architecture du bot CRT/Impulsion (client Deriv robuste, suivi
des trades, garde-fous, graphique et boutons Telegram, backtest) mais **aucune de ses stratégies**.

| Code (réglages) | Stratégie | Unités de temps |
|---|---|---|
| S1 | Trend Pullback | H4 > H1 > M15 |
| S2 | Asian Breakout | M15 |
| S3 | Sweep + CHoCH | M15 > M5 |
| S4 | Mean Reversion | H1 > M15 |

Le bot **n'exécute aucun ordre** : il envoie des alertes (entrée, stop, TP1 à TP3 avec ratio, lot suggéré,
graphique) et suit leur résultat en R. Aucune stratégie n'est garantie ; les paramètres sont des points de
départ à valider par backtest.

## Actifs suivis

| Actif | Symbole Deriv |
|---|---|
| Or | `frxXAUUSD` |
| Majeures | `frxEURUSD`, `frxGBPUSD`, `frxUSDJPY`, `frxUSDCHF`, `frxAUDUSD`, `frxUSDCAD`, `frxNZDUSD` |
| Cryptos | `cryBTCUSD`, `cryETHUSD` |

Les quatre stratégies s'appliquent à chaque actif avec les mêmes règles (toutes les distances sont exprimées en ATR,
donc elles s'adaptent à la volatilité de chaque paire). Elles ont été pensées pour l'or : **rien ne dit qu'elles
fonctionnent aussi bien sur les majeures**. Compare actif par actif au backtest et retire ceux qui ne tiennent pas
(variable `SYMBOLS`). Le suivi de l'exposition traite l'or comme une position sur l'USD : acheter l'or compte
comme vendre l'USD, et 2 trades au plus dans le même sens USD sont ouverts à la fois. BTC et ETH forment un
groupe corrélé : 2 trades au plus dans le même sens.

**Cryptos (marché 24h/24, 7j/7)** : pas de rollover ni de fermeture hebdomadaire, donc aucune fenêtre sans alerte
liée au week-end (seules les annonces USD à fort impact bloquent les alertes) ; les stratégies Sweep + CHoCH et
Mean Reversion ne sont pas limitées aux heures de session. Lot suggéré : 1 unité de la crypto par lot, à vérifier
sur ta plateforme (`LOT_UNITS_CRYBTCUSD` pour un autre contrat). Spread par défaut du backtest : 0,05 %.

## Fichiers

| Fichier | Rôle |
|---|---|
| `gold.py` | Scan, client Deriv (limites de requêtes, reconnexion), alertes et commandes Telegram, suivi |
| `strategies.py` | Les 4 stratégies, indicateurs (EMA, ATR, ADX, RSI, Bollinger, swings), sessions |
| `trend.py` | Tendance et âge de la tendance (D1, H4, H1) pour le récapitulatif journalier |
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
   - `SYMBOLS` : actifs suivis, ex. `frxXAUUSD,frxEURUSD` (défaut : l'or, les 7 majeures, BTC et ETH).
4. Le workflow n'a pas de `schedule` : déclenche-le toutes les 5 minutes avec ton cron externe
   (`workflow_dispatch`), comme pour l'ancien bot. Un scan télécharge M5 et M15 pour chaque actif (10 actifs : une
   quinzaine de secondes avec la temporisation anti-limite de requêtes).
5. Lance une fois le workflow avec `test = true` : tu reçois un message de contrôle avec l'entonnoir.

Les fichiers d'état (`cache.json`, `state.json`, `trades.json`, `guard.json`) sont conservés par le cache GitHub.
Pour les jours d'annonces non couverts par le calendrier automatique, copie `news.example.json` en `news.json`.

## Récapitulatif de tendance journalier

Chaque jour, à la première passe après **17h15 New York** (clôture de la journée de trading), le bot envoie un message
avec, pour chaque actif, la tendance en **D1, H4 et H1**, son **âge** et une phrase de synthèse sur l'alignement :

```
XAUUSD
  D1  🟢 Haussière · établie (2 jambes, 9 j) ⚡ · invalidation 2641.20
  H4  🟢 Haussière · mûre (4 jambes, 6 j) · invalidation 2655.80
  H1  🔴 Baissière · naissante (1 jambe, 14 h) · invalidation 2668.90
  → D1 et H4 alignés (haussiers), H1 à contre-sens (baissiers)
```

Âge = nombre de jambes consécutives (sommet **et** creux plus hauts pour une hausse, plus bas pour une baisse) :
naissante 1 · établie 2-3 · mûre 4 et plus. Deux swings suffisent pour reconnaître une tendance (pas trois, qui arrive
trop tard). La structure doit être intacte (aucune clôture au-delà du dernier creux/sommet), le sommet précédent doit
être cassé par une **clôture**, et ⚡ signale une cassure impulsive. L'invalidation est le dernier creux (hausse) ou
sommet (baisse). Les actifs dont le marché est fermé (forex le week-end) sont listés en bas du message.
La commande `/tendance` renvoie le récapitulatif à la demande (au prochain scan, sous 5 minutes). Réglages dans
`trend.py` (`AGE_ESTABLISHED`, `AGE_MATURE`, `BOS_BY_CLOSE`). Le récapitulatif informe : aucune alerte de trade n'en dépend.

## Tendance et âge dans chaque alerte

Chaque alerte ajoute deux lignes d'information : la tendance **D1** et **H4** de l'actif avec son âge (même lecture que
le récapitulatif), marquées ✅ si elle va dans le sens du trade et ⚠️ si elle est à contre-sens :

```
✅ Tendance D1 haussière · établie (2 jambes, 9 j)
⚠️ Tendance H4 baissière · mûre (4 jambes, 6 j) : contre le trade
```

Ce n'est **pas un filtre** : l'alerte part quelle que soit la tendance. Le backtest enregistre ces deux informations
(colonnes `tend_d1` et `tend_h4` du rapport, ex. « mûre avec », « naissante contre ») pour que tu compares la
performance selon l'âge de la tendance avant d'en faire éventuellement un filtre.

## Commandes Telegram

Boutons **Pris / Ignoré** sous chaque alerte, et : `/pris N`, `/ignore N`, `/r N X` (ton résultat réel du
trade #N en R, ex. `/r 12 -0.8`), `/bilan` (30 derniers jours, par stratégie), `/tendance` (récapitulatif de tendance). Les commandes sont lues à chaque
scan (réponse sous 5 minutes).

## Backtest

Workflow **Backtest Or** (ou en local : `python backtest.py --days 120 --split 0.6`).

- Rejoue les mêmes fonctions que le bot en direct, sans regarder le futur, avec un spread (défaut : or 0,015 %, majeures 0,006 %, BTC/ETH 0,05 % ; `--spread-pct` pour imposer le tien).
- `--sym frxXAUUSD,frxEURUSD` pour choisir les actifs, `--sym all` pour les 10 actifs.
- Rapport par actif, par stratégie, sens, session, alignement H4, résultat, et stabilité entre deux périodes.
- `--only S2,S3` pour une stratégie ; `--set strategies.S1_ADX_MIN=15,18,22` pour une grille ; `--source csv`
  pour tes propres données (un seul actif) ; `--source sim` pour un contrôle de bon sens sur marche aléatoire.
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
