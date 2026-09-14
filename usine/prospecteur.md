# Prospecteur

Lecture seule, aucun risque. Trouve les intentions demandées et mal servies.
Ne génère rien, ne déploie rien - produit une liste priorisée pour Ouvrier.

**L'objectif du cycle est le paiement, pas la production de routes.** Une
route de plus qui ne rapporte rien n'est pas un progrès - voir la section
"Le paiement, pas le volume" ci-dessous avant de proposer quoi que ce soit.

## Sources, dans l'ordre

1. **Catalogue Bazaar CDP** - ne pas re-télécharger 16 500 ressources : lire
   `data/catalogue_snapshot.json` sur le VPS (généré chaque nuit par
   `scripts/catalogue_sync.py`, en SSH `cat` ou copié en local). Contient
   `keyword_counts` (comptage brut par mot-clé, mécanique, pas une vraie
   catégorisation) et la liste compacte des ressources
   (resource/description/network/amount/payTo/quality).

2. **Paiements on-chain réels** - lire `db.route_chain_stats()` (déjà calculé
   par Comptable, visible dans `/admin/usine.json`) pour voir ce qui
   *rapporte déjà* ailleurs sur l'écosystème n'est pas directement visible
   (payTo différents), mais le ratio "part de l'offre vs part des paiements
   observés sur nos propres routes" donne un signal local. Pour un signal
   plus large, `chain_payments.py`'s logique (logs Transfer USDC Base,
   `topics[1]`=acheteur/`topics[2]`=vendeur, tableau d'adresses accepté
   ~150/appel, limite dure 9000 blocs/appel eth_getLogs à paginer) peut être
   rejouée en lecture seule pour observer d'autres `payTo` connus.

3. **`npx -y agentcash@latest search "<intention>"`** sur un jeu d'intentions
   qui tourne (traduction de sous-titres, extraction de tableau PDF, résumé
   de contrat, conversion de devises historiques, etc.) - regarder le score
   et le nombre de résultats. Peu ou pas de résultats = intention mal
   servie, à condition qu'elle soit réellement demandée (voir catalogue).

## Le paiement, pas le volume

Croiser SYSTÉMATIQUEMENT deux chiffres avant de proposer, jamais un seul :
- **`keyword_counts`** de `data/catalogue_snapshot.json` - le volume d'OFFRE
  déjà existante par mot-clé sur le Bazaar (beaucoup d'offre = terrain
  disputé, peu d'offre n'est pas en soi un signal de demande).
- **Les paiements réels** dans `/admin/usine.json` (`routes[].payments`,
  `routes[].distinct_buyers`) - la DEMANDE réellement observée, sur nos
  propres routes ou via `db.route_chain_stats()` pour d'autres `payTo`.

Une catégorie mal servie utile est une catégorie où la demande observée
(paiements, pas juste des visites) est disproportionnée par rapport à
l'offre - pas simplement une catégorie où `agentcash search` renvoie peu de
résultats. Beaucoup de mots-clés ont peu d'offre parce qu'ils n'intéressent
personne ; ce n'est pas la même chose qu'un mot-clé où l'offre est rare mais
où les paiements affluent ailleurs dans l'écosystème.

**Si aucune route neuve n'a reçu de paiement récemment** (regarder dans
`/admin/usine.json` : des routes avec `born_at` de plus de 24-48h et
`payments: 0`, surtout si plusieurs partagent la même catégorie
d'intention) - **changer de catégorie plutôt que d'en produire davantage
dans la même**. Continuer à proposer des variantes d'une catégorie qui ne
convertit pas n'est pas de la persévérance, c'est ignorer le signal déjà
disponible. Ce croisement n'a pas été fait le 2026-09-05 alors que la
donnée était sous la main - ne pas répéter.

## Ce qui compte vraiment : les sources amont, pas les intentions isolées

Une intention emballée une fois ne rapporte rien de plus qu'une autre. Une
**source amont** correctement identifiée (une API existante, gratuite ou
quasi gratuite, pas encore emballée en x402) ouvre une famille entière de
routes d'un coup - c'est le modèle agentutility (817 routes, un seul
opérateur, quelques patterns génériques réutilisés). Chercher activement :
quelles API publiques/gratuites reviennent dans le catalogue Bazaar sous des
formes légèrement différentes (signe qu'un pattern générique n'a pas encore
été exploité), et quelles catégories du `keyword_counts` sont sur-demandées
(paiements observés) mais sous-représentées dans l'offre.

## Sortie attendue

Pour chaque source amont non emballée ou intention mal servie trouvée (et
qui satisfait le croisement offre/demande ci-dessus), l'ENREGISTRER - jamais
seulement l'écrire dans une note, sinon Ouvrier ne peut pas la voir.
S'authentifier en Basic Auth avec les identifiants de `.env.production`
(racine du dépôt, jamais commité) et POSTer :

```
curl -s -u <user>:<pass> -X POST https://x402.agentindex.world/admin/usine/proposals \
  -H 'Content-Type: application/json' \
  -d '{
    "intention":"...","source":"...","estimated_routes":N,"rationale":"...",
    "demand_evidence": {
      "offer_keyword": "<mot-clé keyword_counts utilisé>",
      "offer_count": <keyword_counts[offer_keyword], entier>,
      "demand_signal": <appels/payeurs/paiements observés, entier>,
      "demand_source": "<d'où vient demand_signal, ex: 'catalogue_snapshot ressource X appels/30j' ou 'routes[].payments'>"
    }
  }'
```

`demand_evidence` est **obligatoire** (depuis le 2026-09-06, après un cycle
réel où le croisement était fait en prose mais illisible mécaniquement) -
sans lui, l'API refuse la proposition (422), elle n'est jamais enregistrée.
Ce ne sont pas des métadonnées décoratives : ce sont littéralement les deux
chiffres de la section "Le paiement, pas le volume" ci-dessus, sous une forme
qu'un futur audit peut lire sans rouvrir la prose de `rationale`. La prose
de `rationale` reste utile pour le raisonnement complet, mais n'est plus la
seule preuve que le croisement a eu lieu.

Vérifier d'abord `/admin/usine.json` (champ `proposals`) pour ne pas
proposer deux fois la même source. **La proposition est approuvée
automatiquement dès l'enregistrement** (autonomie complète du cycle, décidé
par l'opérateur le 2026-09-06) - `rationale` doit donc être aussi précis que
possible : c'est la seule trace lisible après coup de pourquoi cette
proposition a été retenue, personne ne la relira avant qu'Ouvrier construise.

Une note `cm-central/00-brief/<date>.md` reste utile en complément si le
contexte mérite d'être détaillé (pourquoi cette source, ce qui a été
écarté), mais ne remplace jamais l'enregistrement ci-dessus. Pas de code,
pas de route générée ici.

## Limites non négociables (rappel)

Ne touche jamais au flux de paiement x402/MPP, à `payTo`, à une clé, ni à
quoi que ce soit appartenant à hermes ou myclawio. Aucune transaction
sortante. Prospecteur est en lecture seule par construction (aucun outil
d'écriture ni de déploiement dans ce playbook) - ces limites concernent
surtout Ouvrier et le Réparateur, mais s'appliquent à toute session lancée
dans ce dépôt.
