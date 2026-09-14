# L'usine à endpoints x402

Six rôles, deux façons de tourner. Voir `enchanted-dancing-dream.md` (plan
d'implémentation) pour le contexte complet et les décisions d'architecture.

## Répartition

**Mécaniques — VPS, cron, zéro appel de modèle, tournent sans supervision :**
- **Contrôleur** (`scripts/controleur.py`) — teste toutes les routes contre
  les vrais outils externes (agentcash, CDP validate, sonde 5 méthodes,
  /sample, taille d'en-tête). Refuse, ne publie jamais rien lui-même.
- **Comptable** (`scripts/comptable.py`) — lit la chaîne, jamais les
  compteurs des tiers. Calcule paiements/acheteurs/retour/part mécanique par
  route.
- **Fossoyeur** (`scripts/fossoyeur.py`) — tourne sur l'**hôte** du VPS (pas
  dans le conteneur, voir le commentaire en tête du script pour pourquoi),
  retire les routes mortes (30 j, 0 paiement) et redéploie.

**Jugement — PC, session Claude Code lancée à la main :**
- **Prospecteur** (`usine/prospecteur.md`) — trouve les intentions mal
  servies.
- **Ouvrier** (`usine/ouvrier.md`) — fabrique la route (registre + code si
  besoin), déploie en SSH.
- **Crieur** (`usine/crieur.md`) — fait passer Contrôleur en verrou de
  publication, inscrit, vérifie l'indexation.

Le tableau de bord `/admin/usine` (même auth que `/admin`) montre l'état des
six, la chaîne de production, les routes, le journal, et le compteur
RÈGLEMENTS TENTÉS.

## Comment lancer une session de jugement

Ouvrir Claude Code dans ce dépôt (`~/dev/clients/agentindex-x402`), puis dans
l'ordre : coller `usine/prospecteur.md`, laisser tourner, lire le résultat ;
coller `usine/ouvrier.md` pour la ou les intentions retenues ; coller
`usine/crieur.md` pour publier. Rien n'empêche de faire les trois en une
seule session si le temps le permet - ce sont des playbooks, pas des
processus séparés à isoler artificiellement.

## Point de reprise entre deux sessions

Ces trois rôles ne tournent PAS en continu (PC potentiellement éteint entre
deux sessions). Avant de terminer une session qui n'a pas fini son travail,
écrire dans `cm-central/00-brief/<date>.md` un point de reprise court :
contexte, ce qui est fait, ce qui reste, la prochaine question à trancher.
La session suivante le lit avant de repartir - jamais de redémarrage à zéro,
jamais de répétition d'une action déjà faite (surtout une inscription ou un
déploiement : rejouer `agentcash register` est inoffensif, redéployer une
route déjà correcte ne l'est pas forcément si les prix ont divergé entre
temps).

## Format du registre

Voir `app/generated/registry.py` (dataclass `RouteSpec`) et
`app/generated/routes_registry.yaml`. Chaque champ y est commenté. Le
`handler_type` générique `http_proxy` couvre la plupart des cas
("emballer une source amont") - un type `custom` avec son propre module
Python n'est justifié que si le générique ne suffit pas (transformation
réelle des données, pas juste un passthrough).

## Garde-fous (rappel)

- Jamais toucher au payTo, au flux de paiement x402/MPP existant, ni à une
  clé.
- Jamais de fausse activité pour gonfler un classement (pas de route
  factice, pas d'appel simulé).
- Le Contrôleur ne se contourne jamais, même pour publier vite.
- Ne rien déployer sur hermes, myclawio, Brami, ou un projet Malik.
