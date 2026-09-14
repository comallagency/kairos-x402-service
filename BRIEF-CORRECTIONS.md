# Corrections et décisions vs. le brief maître initial

Ce fichier existe pour ne pas reperdre ce qu'on a appris en le construisant.
Le "brief maître" original (fourni en prompt, pas versionné) reste la référence
pour tout ce qui n'est pas listé ici.

## Décision produit : pas de Google Places, jamais

**Route A remplacée : `POST /places/search` → `POST /search`** (recherche web + extraction).

Décision structurelle de Comall (2026-09-05), pas un délai : Google impose une
carte bancaire + un compte de facturation actif pour générer une clé, même sous
le palier gratuit, sans plafond de dépense dur (les alertes budgétaires préviennent
après coup, n'arrêtent rien). Sur une route publique appelée par des agents, une
boucle produit une facture découverte trop tard. Refusé par principe.

`/search` réutilise le même moteur que le worker de jobs (`app/upstream/websearch.py`,
plugin OpenRouter `web` engine `parallel` mode `turbo`) exposé en synchrone :
- Entrée : `query`, `max_results` (défaut 5, plafond 10), `extract` (défaut true),
  `summarize` (défaut false)
- Sortie : `title`, `url`, `extract`, `date` (souvent absent en pratique - voir
  ci-dessous), + `x402_receipt` avec `searches_run` et `sources_read`
- Coût réel mesuré : **~0,001 $ par appel, quel que soit `max_results`** (le coût
  du plugin est FORFAITAIRE par appel, pas par résultat retourné - vérifié le
  2026-09-05 : `usage.cost` restait à 0.001 avec `max_results=7`). Vendu 0,10 $.
- Un seul amont (OpenRouter), un seul compte, une seule dépense possible : solde
  vide = tout s'arrête proprement (402/502), aucune facture surprise possible.

Conséquence : plus de notion de quota mensuel amont à surveiller/exposer au
dashboard pour cette route - seul le solde OpenRouter compte (déjà prévu via
`GET /api/v1/key`).

## Bug réel : le tableau `models` d'OpenRouter est plafonné à 3

`POST /chat/completions` avec plus de 3 entrées dans `models` renvoie
`400 'models' array must have 3 items or fewer'`. Le brief listait 4 modèles de
fallback (3 `:free` + `openrouter/free` en dernier recours) dans la même liste -
impossible tel quel.

**Corrigé** : `config.OPENROUTER_TRANSLATE_MODELS` ne contient que les 3 premiers
modèles (fallback natif OpenRouter, un seul appel). `config.OPENROUTER_LAST_RESORT_MODEL`
(`openrouter/free`) est tenté séparément, uniquement si les 3 premiers échouent
tous - voir `app/upstream/openrouter.py::chat_completion_with_fallback()`. Toutes
les routes (`/search`, `/translate`, `/jobs`) passent par ce helper, jamais par
`chat_completion()` directement avec plus de 3 modèles.

Validé en réel le 2026-09-05 : `/translate/sample` a effectivement basculé de
`z-ai/glm-5.2:free` (indisponible ce jour-là) vers `minimax/minimax-m3:free`.

## Chemins d'import réels de la lib `x402==2.22.0`

Le brief donnait les bonnes signatures de fonctions/classes mais de mauvais
chemins d'import (probablement une version différente ou une doc générique).
Chemins vérifiés par introspection :

- `x402.server.x402ResourceServer` (pas `x402.resource_server`)
- `x402.http.types.PaymentOption`, `RouteConfig` (pas `x402.types`)
- `x402.http.middleware.fastapi.PaymentMiddlewareASGI` (pas `x402.asgi.middleware`)
- `x402.extensions.bazaar.{bazaar_resource_server_extension, declare_discovery_extension, OutputConfig}`
- `cdp.x402.create_facilitator_config` vient du package **séparé `cdp-sdk`**,
  absent de la commande pip du brief - ajouté dans `pyproject.toml`.
- Étape manquante dans le brief : en plus du facilitateur, le serveur doit
  enregistrer le scheme EVM `exact` lui-même :
  `x402.mechanisms.evm.exact.register.register_exact_evm_server(server, networks=...)`.
  Sans ça, `x402ResourceServer.initialize()` échoue avec
  `RouteConfigurationError: No scheme implementation registered for "exact"`.

## Garde-fou CDP ajouté (pas dans le brief)

`app/x402_setup.py::build_resource_server()` : sans `CDP_API_KEY_ID`/`CDP_API_KEY_SECRET`,
l'app refuse de démarrer si `ENVIRONMENT=production` (évite de servir gratuitement
par oubli de config). En dev (`ENVIRONMENT` absent ou différent), bascule sur
`app/dev_facilitator.py::LocalDevFacilitatorClient` - répond à `get_supported()`
pour que le challenge 402 se construise, mais `verify()`/`settle()` échouent
toujours (aucun vrai paiement ne peut passer par ce chemin).

## Dashboard : ce que le SDK x402 ne loggue jamais

`PaymentMiddlewareASGI` court-circuite lui-même les requêtes non payées (402) -
il ne consomme même pas le corps de la requête et n'appelle jamais notre code.
Sans intervention, l'app ne voit donc JAMAIS les 402 "intentions" que le brief
veut collecter. Ajouté `app/intent_logging.py` : un middleware ASGI placé entre
`CapacityGateMiddleware` (le plus extérieur) et le middleware de paiement, qui
draine le corps de la requête EN AMONT (sinon `receive()` n'est jamais appelé
par le SDK quand il rejette), le rejoue vers l'app, puis loggue en `status=unpaid`
si la réponse finale est un 402.

`app/db.py::availability_since()` compte les lignes de heartbeat effectivement
écrites sur la fenêtre vs le nombre attendu (pas une moyenne d'un champ `ok`) -
un process mort n'écrit aucune ligne, donc une simple moyenne ne verrait jamais
les coupures complètes. Vérifié en conditions réelles : les redémarrages répétés
du serveur pendant les tests ont fait chuter la dispo mesurée à 51% au lieu de
rester à 100%.

`chain_payments.py` et `send_daily_report.py` sont câblés et tournent sans
erreur, mais n'ont pas encore de données réelles : le premier attend un vrai
`X402_PAY_TO` (refuse de tourner tant que c'est l'adresse placeholder), le second
attend `SMTP_USER`/`SMTP_APP_PASSWORD`/`REPORT_EMAIL_TO` (aucun fournis) - en
attendant il imprime le rapport au lieu de l'envoyer, plutôt que d'échouer.

## Test Sepolia bout-en-bout réussi (2026-09-05) - bug réel trouvé

Rejoué la chaîne complète en réel : wallet de test jetable généré localement
(`eth_account`), financé en vrai via le faucet CDP (`cdp.evm.request_faucet`,
`network="base-sepolia"`, `token="usdc"` - 1.0 USDC reçu), paiement construit et
signé par `x402Client` + `x402HttpxClient` (gère tout seul le cycle 402 → signature
→ retry), réglé par le vrai facilitateur CDP authentifié. 3 paiements réels de
0,10 $ chacun, retrouvés on-chain par `chain_payments.py` avec le bon montant,
la bonne adresse et le bon timestamp de bloc. Script rejouable :
`scripts/sepolia_e2e_test.py` (nécessite le serveur lancé en local).

**Bug réel trouvé et corrigé** : `extract_payer_address()` cherchait le paiement
dans le header `X-PAYMENT` (nom du header dans la doc générique x402). Le vrai
header envoyé par le client x402 est **`Payment-Signature`** (`x-payment` n'est
qu'un fallback légataire côté serveur, jamais ce que le client envoie en
pratique) - confirmé en inspectant `request.headers.keys()` sur une vraie requête
payée. Sans ce correctif, `payer` restait `None` sur 100% des paiements réels,
donc "acheteurs distincts" et "taux de retour" au dashboard restaient à zéro
malgré des paiements réels reçus.

USDC de test Base Sepolia : `0x036CbD53842c5426634e7929541eC2318f3dCF7e` (obtenu
via `x402.mechanisms.evm.default_assets.get_default_asset()`, pas codé en dur).

## Déploiement VPS (2026-09-05)

- Certbot sur ce VPS n'a **pas** le plugin nginx (`python3-certbot-nginx` absent,
  seuls `standalone`/`webroot` dispo) - contrairement à ce qu'on pourrait
  supposer en voyant les vhosts MyHermes. Utilisé `certbot certonly --webroot
  -w /var/www/x402-acme -d x402.agentindex.world`, webroot dédié à x402 (pas
  celui de MyHermes), même schéma en 2 fichiers (HTTP redirect + HTTPS) que les
  vhosts existants.
- Bug réel : le premier build Docker utilisait `useradd -m -u 1000 appuser`,
  mais les volumes montés (`./data`, `./logs`) sont sur l'hôte, possédés par
  `x402` (uid **1003**) - uid 1000 n'y avait pas accès en écriture
  (`sqlite3.OperationalError: unable to open database file` au démarrage).
  Corrigé : uid du user interne au conteneur alignée sur l'uid hôte (1003).
- Port interne choisi après vérif `ss -tlnp` : **127.0.0.1:18402** (loopback
  uniquement, jamais exposé directement) - aucun conflit avec les ports MyHermes
  (17100-17102, 18080, 19100-19102) ni MyClawIO.
- `docker-compose.yml` attend `./data` et `./logs` **à l'intérieur** de
  `/opt/x402/app/` (pas `/opt/x402/data` comme la l'arbo initiale du provisioning
  utilisateur le suggérait) - créés à cet endroit, dossiers d'origine laissés
  vides sans conséquence.
- Cron `x402` (crontab dédié, pas celui de root) : `chain_payments.py` toutes
  les heures, `send_daily_report.py` à 07:00, tous deux via
  `docker compose run --rm x402 python ...` pour réutiliser l'image sans
  environnement Python séparé sur l'hôte.
- **Paiement réel rejoué en HTTPS derrière nginx** (pas juste localhost) :
  succès du premier coup, aucun souci de header/timeout côté proxy. Toujours
  sur Sepolia (`X402_NETWORK=eip155:84532`) - bascule mainnet non faite,
  décision explicite en attente.

## Bug réel : le dashboard mélangeait Sepolia et mainnet (2026-09-05)

Trouvé par l'utilisateur en comparant à l'état réel on-chain (`payTo` à 0,00 $ sur
Base mainnet) contre le dashboard qui affichait 0,10 $ de revenu et 1 acheteur.
Cause : `revenue_usdc`/`buyers`/`last_payment_at` étaient calculés depuis la
table `requests` (statut `paid` autodéclaré par l'app), qui ne distinguait
jamais le réseau — les paiements de test Sepolia (avant la bascule mainnet)
restaient comptés indéfiniment comme du vrai revenu.

**Corrigé, sur directive explicite** ("chain_payments.py lit déjà le mainnet,
c'est la source à privilégier") :
- Colonne `network` ajoutée à `requests` ET `chain_payments`, migration
  idempotente avec backfill (tout ce qui existait avant la colonne = Sepolia,
  puisque la bascule mainnet n'avait pas encore eu lieu).
- Les chiffres financiers (`revenue_usdc`, `buyers`, `last_payment_at`,
  `chain`) viennent maintenant exclusivement de `chain_payments` filtré par
  `network = config.X402_NETWORK` - jamais de la table `requests`.
- `collect_dashboard_data()` déclenche un `chain_payments.sync()` en direct à
  chaque chargement du dashboard (pas seulement le cron horaire), pour éviter
  un décalage entre un vrai paiement et son apparition dans les chiffres.
- Bloc "Testnet" séparé et étiqueté (bordure pointillée, couleur distincte)
  affichant les mêmes métriques pour Sepolia, seulement si le réseau actif
  n'est pas déjà Sepolia.
- Vérifié en réel après déploiement : mainnet affiche bien 0,00 $ / 0 acheteur
  / aucun paiement, le bloc testnet affiche les 4 paiements Sepolia réels
  (0,40 $, 4 acheteurs) séparément.

**Limite connue, mineure, assumée** : les logs `requests` non-monétaires
(`unpaid`/`error`, pas `paid`) créés dans la fenêtre entre la bascule mainnet
et le déploiement de la colonne `network` (~14 min ce jour-là) ont été
rétroactivement classés Sepolia par le backfill alors qu'ils étaient déjà du
trafic mainnet. Zéro impact financier (aucun n'est `paid`), juste un léger
mécompte cosmétique dans le tableau "Appels par route" pour cette fenêtre
précise - pas corrigé, l'horodatage exact de la bascule n'étant pas stocké.

## Bug réel : les tags Bazaar n'atteignaient jamais le well-known (2026-09-05)

x402scan affichait le champ "Tags" vide sur la fiche malgré `tags=[...]` bien
présent dans chaque `RouteConfig` (`app/x402_setup.py`). Cause : `app/discovery.py`
reconstruit à la main le JSON de `/.well-known/x402` (par design, pour ne
jamais dériver de la config réelle du middleware de paiement) et avait oublié
de recopier `route_config.tags` (et `service_name`) dans l'entrée - corrigé.
Toujours vérifier que `discovery.py::_route_entries()` reflète bien TOUS les
champs pertinents de `RouteConfig` à chaque ajout de champ futur.

## x402 Arena : schéma réel de `POST core.x402arena.gg/register`

Non documenté publiquement (page d'accueil et `/register` bloquées par
Cloudflare pour les user-agents non-navigateur - fonctionne avec un
`User-Agent` de navigateur classique). Champs découverts par tâtonnement :
`name` (slug : 3-50 car., minuscules/chiffres/tirets uniquement - PAS le nom
d'affichage), `endpoint` (URL complète), `method` (**obligatoire** si la route
n'est pas GET - sans lui, leur vérificateur fait un GET nu, reçoit 405 sur nos
routes POST-only, et rejette avec `statusReceived:405`). Réponse succès (201) :
confirme network/payTo/pricing en les relisant depuis notre vrai challenge 402.
Trois agents enregistrés : `agentindex-x402` (/search), `agentindex-translate`,
`agentindex-jobs`.

## ampersend : statut d'ingestion non vérifiable par API

`app.ampersend.ai/discover` est une SPA Next.js entièrement rendue côté client
- toutes les routes `/api/*` testées (discover, services) renvoient le HTML de
la coquille de l'app, pas une réponse JSON. Impossible de confirmer par ce
biais si le well-known a déjà été ingéré. Reste à vérifier manuellement dans
un navigateur avant d'écrire quoi que ce soit côté ampersend.

## Publication registre MCP officiel sous `world.agentindex/x402` (2026-09-05)

**Bug de doc réel** : plusieurs sources (dont une doc du repo `registry` elle-même,
citant un exemple réel `_mcp-registry.letta.com`) laissent penser que le TXT de
vérification DNS va sous `_mcp-registry.<domaine>`. **Faux avec la version
actuelle du registre (v1.8.1)** - vérifié en lisant le vrai code source
(`internal/api/handlers/v0/auth/dns.go`) : le lookup se fait sur l'**apex**
du domaine, et le code contient même une détection explicite de cette erreur
(`commonWrongSelectors = ["_mcp-auth", "_mcp-registry"]`) qui renvoie un
message d'aide si le TXT est trouvé au mauvais endroit. Ne jamais faire
confiance à la doc/aux blogs sur ce point précis - relire le code source de
la version installée si un doute existe.

Un TXT à l'apex ne casse rien : coexiste sans conflit avec le SPF déjà présent
au même endroit (types de record différents, même nom).

**Étapes réellement exécutées** :
1. Génération d'une paire de clés Ed25519 via `openssl genpkey`, clé privée
   stockée dans `.mcp-registry-keys/` (chmod 600, jamais committée).
2. TXT `v=MCPv1; k=ed25519; p=<clé publique b64>` posé à l'apex par Comall
   (Namecheap), vérifié propagé sur 8.8.8.8 et 1.1.1.1 avant de continuer.
3. `mcp-publisher login dns --domain agentindex.world --private-key <hex>`
4. `server.json` à la racine du repo : `remotes` de type `streamable-http`
   vers `https://x402.agentindex.world/mcp/` (slash final - évite la
   redirection 307 que fait fastmcp sur `/mcp` sans slash). **Le champ
   `description` du schéma officiel est plafonné à 100 caractères** - impossible
   d'y lister les 3 outils et leurs prix ; c'est voulu par le design du
   registre, ce détail vit dans le serveur MCP lui-même (chaque outil de
   `app/mcp_server.py` a déjà sa description complète avec prix).
5. `mcp-publisher publish` - succès, confirmé visible via
   `GET https://registry.modelcontextprotocol.io/v0/servers?search=agentindex`
   (`status: active`, `isLatest: true`).

CLI et clés conservés dans le repo (`.tools/mcp-publisher`,
`.mcp-registry-keys/`), tous deux dans `.gitignore` - nécessaires pour publier
une future version 1.0.x sans tout regénérer.

## Descriptions réécrites - ne plus révéler le fournisseur amont (2026-09-05)

Décision de Comall : les descriptions initiales nommaient OpenRouter, le
plugin web, le mode "turbo" et des modèles `:free` explicites - ça donne aux
acheteurs le mode d'emploi pour nous contourner, et "free" sur /translate
détruit la justification du prix (aucun agent ne paye 0,10 $ un appel qu'on
lui dit gratuit derrière). Réécrites pour décrire uniquement ce que l'acheteur
reçoit et pourquoi il paye - zéro mention de fournisseur, de mode d'appel ou
de modèle. `ROUTE_DESCRIPTIONS` dans `app/x402_setup.py` reste la seule
source ; vérifié en réel après déploiement que le texte est **identique** dans
le challenge 402, `/.well-known/x402`, `/openapi.json`, `/agent.json` et les
3 descriptions d'outils MCP.

Corrigé au passage : `TRANSLATE_SAMPLE_OUTPUT` (l'exemple statique embarqué
dans la déclaration Bazaar, pas la vraie réponse de `/translate/sample`)
avait `"model_served": "z-ai/glm-5.2:free"` en dur - un exemple de
démonstration n'a aucune raison de citer un vrai modèle avec son suffixe
`:free`, changé en `"agentindex-translate-1"`.

Gap réel trouvé : `openapi.json` n'avait **aucune** description sur les 3
routes POST (`description: None` confirmé avant correctif) - les handlers
FastAPI n'avaient jamais reçu de `description=` sur leur décorateur, alors
que `RouteConfig.description` (x402_setup.py) alimentait déjà le 402 et le
well-known. Ajouté `description=ROUTE_DESCRIPTIONS[...]` aux 3 décorateurs
`@router.post(...)`.

**Suite (même jour) - `upstream` et `model_served` neutralisés sur décision
explicite** : `upstream` devient une catégorie de traitement (`"web_search"`
pour `/search` et `/jobs`, `"llm"` pour `/translate`), plus jamais le nom du
fournisseur. `model_served` reste rempli mais renvoie un tag stable et neutre
(`app/receipts.py::neutral_model_id()`, mapping fixe des 4 vrais IDs de
modèles connus vers `model-a/b/c/fallback` - même vrai modèle → toujours le
même tag, sans jamais le nommer). `fallback_used` reste la seule info
"a-t-il basculé" dont l'acheteur a besoin.

Bug réel trouvé en auditant TOUS les appels à `make_receipt()` avant de
corriger : `/search` (HTTP et outil MCP) passait déjà le vrai `model_served`
issu de `run_web_search()` (le plugin web est une completion de chat sous le
capot) - je l'avais raté au premier passage en supposant `None` sans
vérifier. Les 6 sites d'appel (`search.py` x2, `translate.py` x2,
`jobs.py` x2, `mcp_server.py` x2) sont maintenant tous passés en revue et
neutralisés ; le seul `"openrouter"` restant dans le code est dans
`app/admin.py` (dashboard interne, protégé par basic auth, jamais exposé aux
acheteurs - légitime que l'opérateur y voie le vrai fournisseur).

Vérifié en réel après déploiement : `/search/sample`, `/translate/sample`,
`/jobs/sample` ne contiennent plus aucun nom de fournisseur ni de modèle,
recherché sur le corps complet de chaque réponse (pas juste le receipt).

## Enrichissement OpenAPI pour le score "AI-agent readiness" Circle (2026-09-05)

Sur prompt de correction du scanner Circle (32/100). Décision explicite :
documentation OpenAPI uniquement, **zéro changement de logique de paiement**
(le comportement runtime est déjà validé par CDP). Écarté explicitement :
MPP (le compteur `mpp_attempts_24h` reste à 0, rien à construire pour une
demande jamais mesurée), et l'acceptation multi-réseaux (aucune demande
observée, on reste sur Base).

Appliqué, tout dérivé de `build_route_configs()` - jamais écrit en dur, pour
ne pas reproduire le bug de dérive des tags : `app/openapi_custom.py`
post-traite le schéma OpenAPI généré par FastAPI (`app.openapi = ...`,
pattern standard de cache/override) et ajoute, par route payante :
- `x-payment-info` (`price.amount` calculé depuis `PaymentOption.price`,
  jamais recopié)
- une réponse `402` documentée avec un exemple `accepts[]` reconstruit via
  `x402.mechanisms.evm.default_assets.get_default_asset()` (même mécanisme
  que celui utilisé par le SDK pour construire le vrai challenge - pas
  besoin d'un `x402ResourceServer` initialisé, donc pas de dépendance
  réseau/lifecycle au moment de la génération du schéma)
- `info.x-guidance` (817 caractères, largement sous le budget ~1000 tokens)
- `externalDocs.url` vers `/agent.json`

Point d'attention technique : `openapi_extra` sur un décorateur de route
FastAPI ferait un remplacement complet (pas une fusion) de la clé
`responses` si on l'utilisait pour ajouter le 402 - ça aurait effacé le
`200` auto-généré. D'où le choix d'un post-traitement du schéma complet
(`.setdefault("responses", {})["402"] = ...`) plutôt qu'un `openapi_extra`
par route. Validé avec `openapi-spec-validator` (schéma conforme) et vérifié
en réel en production après déploiement (prix corrects : 0.10/0.10/1.00,
`200` toujours présent, aucune régression sur les 402 réels des 3 routes).

Non fait (hors périmètre "document uniquement") : middleware Circle Gateway,
CLI Circle, skills Circle - Comall gère Circle à la main.

## Bug réel : "a cleaned page extract" ne l'était pas (2026-09-05)

Le commentaire d'origine dans `app/upstream/websearch.py` affirmait que le
plugin web d'OpenRouter renvoyait déjà un extrait nettoyé - faux, vérifié sur
donnée réelle : `uc["content"]` est le texte brut scrapé de la page, complet
avec menu de navigation (`* Home\n* Menu\n* Photos\n* Open Times...`), blocs
d'adresse/téléphone répétés verbatim (ex. "Ichiran Shibuya-1" / "-2" avec la
même adresse recopiée), et retours à la ligne bruts - exactement ce que notre
description `/search` promet de ne PAS montrer.

Corrigé avec `_clean_extract()` (heuristique générale, pas spécifique à un
site) : une ligne sans ponctuation de fin de phrase et de ≤4 mots est traitée
comme un libellé d'interface (menu/nav) et supprimée ; les lignes strictement
dupliquées (après normalisation) ne sont gardées qu'une fois ; le reste est
aplati en un paragraphe et coupé à ~500 caractères sur une limite de mot (pas
en plein milieu), avec `...` si coupé. Vérifié sur un vrai appel avant ET
après déploiement, sur le cas Ichiran signalé par l'utilisateur - la nav et
les doublons ont disparu, ne reste que la prose descriptive réelle.

## Mode lot sur /search et /translate (2026-09-05)

`text` (/translate) et `query` (/search) acceptent maintenant soit une chaîne
(comportement historique, inchangé) soit un tableau - jusqu'à 200 segments
pour /translate, 5 requêtes pour /search. Prix inchangé (0,10 $ pour l'appel
entier, jamais par segment/requête). Appliqué de façon identique sur les
routes HTTP et les 3 outils MCP (même fonctions partagées, `str | list[str]`
sur les deux surfaces - FastMCP génère bien un `anyOf` JSON Schema correct à
partir de l'union de types, vérifié par introspection du schéma réel).

**Translate** : un seul appel amont pour tous les segments (prompt demandant
un tableau JSON aligné en sortie), avec un repli explicite si la réponse est
mal formée ou désalignée (`BatchMisaligned`) - un seul nouvel essai, sur le
modèle de secours uniquement, plutôt que de renvoyer un tableau qui ne
correspond pas à l'entrée. Testé en réel à l'échelle annoncée : 200/200
segments alignés, placeholder `{name}` préservé sur toute la série (41s).
Nouveau champ `segments_processed` dans le reçu, uniquement en mode lot.

**Search** : chaque requête reste un appel amont séparé (réutilise
`run_web_search`, déjà éprouvé par le worker de jobs) - "un seul appel"
désigne l'appel du client vers nous, pas notre nombre d'appels amont interne.
Résultats fusionnés et dédoublonnés par URL, plafonnés à `max_results`.
Testé en réel : 3 requêtes → 8 résultats uniques, zéro doublon. `searches_run`
dans le reçu reflète déjà le nombre réel de requêtes exécutées, aucun nouveau
champ nécessaire.

Descriptions réécrites autour de la vraie proposition de valeur (un appel au
lieu de N), toujours <=500 caractères, toujours sans nommer de fournisseur.

Piège de test réel : en environnement de dev (facilitateur factice), toute
requête sans paiement reçoit `{}` du middleware de paiement AVANT même
d'atteindre le handler - donc impossible de tester la validation du lot
(nombre de segments, types) via de vraies requêtes HTTP locales sans payer.
Contourné en appelant les fonctions de handler directement avec un faux objet
`Request` Starlette (bypass total du middleware) - à refaire pour toute
future validation d'entrée sur route payante testée en local.

## Les trois produits, version actuelle

1. `POST /search` - $0.10 - recherche web (remplace Places)
2. `POST /translate` - $0.10 - traduction OpenRouter `:free`
3. `POST /jobs` - $1.00 - recherche longue asynchrone

## Serveur MCP (2026-09-05) - `wrap_fastmcp_tool` inutilisable, implémentation manuelle

Les 3 routes sont maintenant aussi exposées comme outils MCP (`search`,
`translate`, `jobs`) via `app/mcp_server.py`, monté dans `app/main.py` sur
`inner_app` (`inner_app.mount("/mcp", mcp.http_app(path="/"))`), avant les
middlewares de paiement/logging/capacité - vérifié en réel : `GET /mcp` ne
renvoie qu'une redirection 307 (trailing slash Starlette), jamais un 402, les
trois middlewares HTTP ne matchent que les tuples `(méthode, chemin)` exacts
qu'ils connaissent (`ROUTE_KEYS`, `RoutesConfig`).

**Bug réel trouvé (pas un problème de version fastmcp)** : `x402.mcp.server_async
.wrap_fastmcp_tool` fait `from .server import _extract_meta_from_fastmcp_context,
_mcp_tool_result_to_call_tool_result` - ces deux noms n'existent tout simplement
pas dans `x402/mcp/server.py` tel que livré dans `x402==2.22.0`. Confirmé par un
script jetable qui construit un vrai `x402ResourceServer` + `PaymentWrapperConfig`
et appelle `wrap_fastmcp_tool` : `ImportError` immédiat, avant même d'atteindre
fastmcp. Séparément, `x402.mcp.server.create_payment_wrapper` (celui qui
fonctionne isolément) fait `from mcp.server.fastmcp import Context` - le
`FastMCP`/`Context` du SDK MCP officiel (`mcp==2.1.1`, installé comme dépendance
transitive de `fastmcp`), pas `fastmcp.server.context.Context` du paquet
`fastmcp` réellement utilisé ici (`fastmcp==4.0.3`, projet standalone de Jeremiah
Lowin, API différente). Son injection de contexte repose sur
`issubclass(annotation, Context)` : avec un `ctx: Context` du mauvais paquet,
FastMCP 4.x n'aurait jamais rien injecté, sans lever la moindre erreur -
paiement silencieusement toujours absent.

**Corrigé** : `app/mcp_server.py` réimplémente directement le flux verify ->
handler -> settle contre `x402ResourceServer` (méthodes confirmées par
introspection, comme le reste du projet : `build_payment_requirements`,
`create_payment_required_response`, `find_matching_requirements`,
`verify_payment`, `settle_payment` - toutes correctement documentées dans
`server_base.py` malgré le docstring de `create_payment_wrapper` qui référence
`build_payment_requirements_from_config`, méthode qui n'existe pas). Le paiement
voyage dans `_meta["x402/payment"]` de la requête MCP, exposé côté serveur via
`ctx.request_context.meta` (`fastmcp.server.dependencies.FastMCPRequestContext`,
qui "lift" le `_meta` brut des params - vérifié en lisant
`fastmcp/server/dependencies.py`). Un seul `x402ResourceServer` partagé entre
HTTP et MCP (`app.x402_setup.get_resource_server()`, nouveau singleton) pour ne
jamais initialiser deux fois le facilitateur.

**Lifespan** : `mcp.http_app()` a son propre lifespan (démarre le task group du
session manager streamable-HTTP) - sans l'entrer, la première session MCP
plante. Combiné au lifespan existant (`worker_loop` + `heartbeat_loop`) via
`fastmcp.utilities.lifespan.combine_lifespans`, utilitaire officiel du paquet
fait exactement pour ce cas (voir son propre docstring d'exemple, qui montre
exactement le montage `FastAPI(lifespan=combine_lifespans(...)); app.mount(...)`).

**Test réel Sepolia via MCP réussi (2026-09-05)** : `scripts/sepolia_mcp_e2e_test.py`
(même wallet jetable + faucet CDP que `sepolia_e2e_test.py`, mais appel via
`fastmcp.Client` au lieu d'un client HTTP) - `search` appelé sans paiement ->
erreur `x402Version:2` correctement formée avec `accepts` ; paiement signé avec
`x402Client` + `register_exact_evm_client` à partir de ce challenge ; rejoué
avec `meta={"x402/payment": ...}` -> résultat réel (vrais résultats de
recherche), `tx` réel on-chain, réglé avec succès. Vérifié dans `requests` :
ligne `route=search, method=MCP, status=paid, amount_usdc=0.10, payer=<wallet
de test>, network=eip155:84532` - le dashboard voit donc les paiements MCP
exactement comme les paiements HTTP.

**Portée volontairement limitée** : seuls les 3 outils payants (`search`,
`translate`, `jobs`) sont exposés en MCP, comme demandé - pas d'outil MCP pour
`GET /jobs/{id}` (gratuit, polling) : un agent qui utilise l'outil MCP `jobs`
récupère un `job_id` et peut interroger le statut via l'HTTP existant, aucune
raison de dupliquer une route gratuite en MCP pour l'instant.

## Enregistrement au registre MCP officiel sous `world.agentindex` - note de recherche, pas fait

Recherche seule, aucune action prise (pas de DNS touché, pas d'enregistrement
créé) :

- Le MCP Registry officiel (`registry.modelcontextprotocol.io`) authentifie la
  propriété d'un *namespace* de serveur (ex. `world.agentindex/x402`) par
  **DNS ou par propriété de dépôt GitHub** - pour un domaine, il exige un
  enregistrement TXT à `_mcp-registry.<domaine>` (ou similaire selon la version
  de la doc au moment de l'inscription - à revérifier au moment de le faire,
  le format exact a bougé plusieurs fois) contenant un jeton fourni par le
  registre au moment de l'inscription, prouvant le contrôle du domaine
  `agentindex.world` avant que le sous-namespace `world.agentindex/*` ne soit
  accepté.
- L'outil concerné est le CLI officiel `mcp-publisher` (Go, dépôt
  `modelcontextprotocol/registry`) : `mcp-publisher login dns` (ou `github`)
  puis `mcp-publisher publish`, à partir d'un fichier manifeste `server.json` à
  la racine du dépôt du serveur - schéma proche de `package.json`/`pyproject.toml`
  mais spécifique au registre (`name`, `description`, `repository`, `version`,
  et un bloc `packages`/`remotes` décrivant comment lancer/joindre le serveur -
  ici un `remote` de type `streamable-http` pointant sur
  `https://x402.agentindex.world/mcp/`).
- Conséquence pratique pour ce projet : il faudrait (1) décider du nom exact
  du serveur sous le namespace (`world.agentindex/x402` ou équivalent), (2)
  poser le TXT DNS sur `agentindex.world` (accès DNS déjà existant vu le
  certificat Let's Encrypt du sous-domaine `x402.agentindex.world`), (3)
  écrire `server.json`, (4) publier avec `mcp-publisher`. Rien de bloquant
  techniquement, mais volontairement non fait ici - décision produit à prendre
  séparément, hors périmètre de cette tâche.

## Premier appel gratuit par adresse de wallet (2026-09-05)

Mécanisme choisi après introspection directe de la lib x402==2.22.0 (pas de la
doc, qui référence des méthodes qui n'existent pas côté `x402HTTPServerBase` -
`on_protected_request` ne permet pas de "vérifier mais sauter le règlement",
et de toute façon `PaymentMiddlewareASGI` construit son propre
`x402HTTPResourceServer` interne, jamais exposé à l'app) : hook
`x402ResourceServer.on_before_settle()` posé directement sur le singleton
partagé (`app/x402_setup.py::get_resource_server()`), donc valable pour les
deux surfaces HTTP et MCP sans câblage supplémentaire, puisque les deux
consomment déjà ce même singleton.

Le hook (`_first_call_free_hook`) tourne après la vérification cryptographique
normale (signature + fonds réels contrôlés comme d'habitude) et après
l'exécution du handler métier - il ne fait que sauter l'appel de règlement
réel au facilitateur la première fois qu'une adresse est vue, tous produits
confondus (pas par route), puis facture normalement pour toujours ensuite.
Enregistrement atomique en base (`db.mark_wallet_seen()`, `INSERT OR IGNORE` +
`rowcount`) pour qu'un double appel concurrent depuis la même adresse ne passe
jamais deux fois gratuitement. La réponse de règlement simulée ne ment jamais
sur ce qui s'est passé : `transaction=""` (aucun hash fabriqué), `amount="0"`.

Comme le handler construit son reçu avant que le hook ne s'exécute (le flux
x402 est vérifier → exécuter le handler → régler), le prix affiché dans le
reçu vient d'une prévisualisation en lecture seule (`effective_price()`, qui
relit juste `db.has_seen_wallet()`) - elle ne peut pas diverger de la décision
réelle du hook en usage normal, rien d'autre n'écrivant dans `seen_wallets`
entre les deux dans la durée d'une requête.

Descriptions des 3 routes mises à jour avec « first call free per wallet »
comme argument de vente explicite (pas juste une note en petit), en restant
sous la limite de 500 caractères déjà imposée par l'assertion existante.

**Vérifié en réel sur Base Sepolia, pas juste en local avec le facilitateur de
dev** (qui échoue toujours à la vérification et n'atteint jamais le
règlement) : wallet jetable généré et financé via le faucet CDP (1.0 USDC
reçu), deux appels `POST /search` signés à la suite avec la même adresse.
1er appel : reçu `price_paid_usdc: 0.0`. 2e appel, même wallet : reçu
`price_paid_usdc: 0.1`. Preuve on-chain indépendante du reçu : solde du wallet
mesuré directement via `eth_call` avant/après - passé de 1.0 à 0.9 USDC après
les deux appels, soit exactement un seul vrai débit de 0,10 $ malgré deux
appels payés en apparence. Script rejouable :
`scripts/first_call_free_e2e_test.py`.

## Note : bascule mainnet déjà faite pendant cette même journée

En vérifiant le déploiement de la fonctionnalité ci-dessus en production, le
challenge 402 réel de `https://x402.agentindex.world/search` annonçait
`network: eip155:8453` (Base **mainnet**), pas `eip155:84532` (Sepolia)
attendu d'après l'état précédemment noté. Vérification sur le VPS : `.env` du
service (`/opt/x402/app/.env`, modifié le 2026-09-05 14:42) a bien
`X402_NETWORK=eip155:8453` et `ENVIRONMENT=production` - la bascule mainnet a
donc déjà eu lieu plus tôt le même jour, probablement dans le cadre du correctif
du bug dashboard Sepolia/mainnet documenté plus haut. Pas un bug introduit par
cette tâche, mais à corriger dans les mémoires/notes qui disaient encore
« toujours sur Sepolia, bascule non faite ». Conséquence pratique : le test de
paiement réel pour cette fonctionnalité a été fait en local sur Sepolia
uniquement (jamais rejoué en mainnet réel avec de l'argent réel) - la logique
du hook ne dépend pas du réseau (`config.X402_NETWORK` est juste transmis tel
quel), donc aucune raison de penser qu'elle se comporte différemment en
mainnet, mais ça n'a pas été prouvé avec un vrai paiement mainnet.

## Découvrabilité AgentCash (2026-09-05) - bug réel trouvé par lecture du code du validateur

AgentCash (canal de découverte agents avec le plus d'acheteurs financés)
signalait `x402.agentindex.world` invisible. Plutôt que de deviner depuis le
texte des avertissements, le code source du validateur (`@agentcash/discovery`,
récupéré via `npx`, source dans `~/.npm/_npx/*/node_modules/@agentcash/discovery/
dist/index.js`) a été lu directement pour trouver la cause exacte de chaque
code d'erreur - même discipline que pour la lib `x402` elle-même.

**Bug réel, cause racine unique de 4 avertissements** : `x-payment-info.price.currency`
valait `"USDC"` (ticker crypto) au lieu de `"USD"`. Le schéma de validation
d'AgentCash (`Iso4217Schema = z.string().regex(/^[A-Z]{3}$/)`) exige exactement
3 lettres - "USDC" en fait 4, la regex échoue, `PaymentInfoSchema.safeParse()`
rejette alors le bloc `x-payment-info` **en entier** (prix ET protocols,
silencieusement, y compris l'array `protocols` qui était pourtant valide) -
d'où `L2_PRICE_MISSING_ON_PAID` et `L2_PROTOCOLS_MISSING_ON_PAID` sur les 3
routes payantes malgré un bloc `x-payment-info` présent et à moitié correct.
Le champ décrit un prix décimal USD (métadonnée de découverte), pas l'actif
on-chain réellement réglé (USDC sur Base, inchangé) - deux choses différentes
qu'il ne fallait pas confondre. Corrigé dans `app/openapi_custom.py::_x_payment_info`.

**`L3_INPUT_SCHEMA_MISSING` sur les 3 routes payantes** : nos handlers
acceptent un `Request` brut (parsing JSON manuel, pas de modèle Pydantic), donc
FastAPI ne générait jamais de `requestBody` OpenAPI standard - seule
l'extension Bazaar (`x402.extensions.bazaar`) décrivait la forme de l'entrée,
dans un format qu'AgentCash ne lit pas. Les 3 schémas d'entrée existaient déjà
en trois exemplaires légèrement dupliqués (inline dans `build_route_configs()`,
et une deuxième fois dans `app/mcp_server.py` pour les tools MCP) - extraits en
constantes uniques (`SEARCH_INPUT_SCHEMA`, `TRANSLATE_INPUT_SCHEMA`,
`JOBS_INPUT_SCHEMA` dans `app/x402_setup.py`), maintenant la seule source pour
Bazaar, MCP, et le nouveau `requestBody` OpenAPI (`app/openapi_custom.py`) -
plus de risque de dérive entre les trois.

**`L3_AUTH_MODE_MISSING` partout** : `inferAuthMode()` côté validateur ne
reconnaît que 3 signaux - `x-payment-info` présent (paid), `security: []`
explicite (unprotected), ou un `securityScheme` de type `apiKey`/`siwx`
référencé. Un `security` absent (ni vide ni référencé) donne un authMode
indéfini, avertissement inclus. Ajouté `security: []` à `/.well-known/x402`,
`/agent.json`, `/llms.txt`, `/health` (tous génuinement publics). Pour
`/admin`, `/admin/live`, `/admin/data.json` (protégés par HTTP Basic via
`fastapi.security.HTTPBasic`, un schéma que ce validateur ne reconnaît ni
comme `apiKey` ni comme `siwx`) : plutôt que de mentir avec un faux
`securityScheme`, ces 3 routes sont sorties du schéma OpenAPI public
(`include_in_schema=False`) - ce sont des pages d'exploitation internes, pas
des ressources payables par un agent, elles n'ont rien à faire dans un
document que des agents utilisent pour décider quoi appeler.

**`/llms.txt` en 404** : pas exigé par le flux de validation OpenAPI
lui-même (confirmé en lisant `discovery.md` en entier - seul `/openapi.json`
compte pour `discover`/`check`), mais c'est la convention que leur propre
générateur (`@agentcash/router`, code source inspecté via `npm pack`) produit
automatiquement : un simple echo en texte brut du même `info.x-guidance` déjà
présent dans `openapi.json`. Ajouté `GET /llms.txt` dans `app/discovery.py`,
qui renvoie littéralement `X_GUIDANCE` (déjà défini dans `app/openapi_custom.py`
pour `info.x-guidance`) - une seule chaîne, deux endroits où elle est exposée.

**Le `L3_NOT_FOUND` initial ("Not found — no spec data for this endpoint")
n'était pas un vrai problème** : `check <url>` exige un chemin d'endpoint
précis (`/search`, pas juste l'origine) - vérifié en comparant avec
`stableenrich.dev`, une API de référence 100% conforme d'après leur propre
doc, qui donne EXACTEMENT le même `L3_NOT_FOUND` sur son origine nue mais un
résultat propre sur un de ses endpoints réels. Rien à corriger ici, c'est un
détail d'usage de la commande, pas un défaut du service.

**Vérifié avec leur outil officiel, pas une supposition** : `npx -y
@agentcash/discovery@latest discover "https://x402.agentindex.world"` donne
maintenant 13 routes, zéro avertissement (avant : 24). `check` sur `/search`,
`/translate` et `/jobs` donne chacun `paid  <prix> USD  [x402]` sans aucun
avertissement, identique au résultat de l'endpoint de référence
`stableenrich.dev`.

## Premier appel gratuit par wallet : désactivé (2026-09-05)

Raison : le premier paiement d'une nouvelle adresse est précisément
l'événement qui déclenche l'indexation Bazaar CDP et le seul revenu produit
par les sondes automatiques (scanners d'annuaire) - l'offrir gratuitement
retardait indéfiniment ces deux choses pour un argument de conversion qui ne
sert à rien tant qu'on n'est pas encore découvert.

Interrupteur, pas suppression : `config.FREE_FIRST_CALL` (env
`FREE_FIRST_CALL`, défaut `false`) gate la toute première ligne de
`_first_call_free_hook` (`app/x402_setup.py`) et de `effective_price()`
(`app/receipts.py`) - le code du hook, la table `seen_wallets` et le test
`scripts/first_call_free_e2e_test.py` restent intacts. Quand le flag est
`false`, aucun wallet n'est jamais enregistré comme vu non plus (pas d'écriture
dans `seen_wallets`), donc rallumer plus tard traite bien chaque wallet comme
réellement nouveau plutôt que de retrouver l'historique d'avant la coupure.
`FREE_FIRST_CALL=false` ajouté explicitement dans `.env` sur le VPS (et dans
`.env.example` pour la doc). Mention « first call free per wallet » retirée
des 3 `ROUTE_DESCRIPTIONS`.

Vérifié : script de test rejoué en local (wallet neuf, deux appels réels sur
Sepolia) - les deux appels sont désormais facturés normalement (0,10 $ chacun,
`seen_wallets` reste vide), confirmé aussi par un test unitaire direct sur
`effective_price()` dans les deux états du flag. Prod redéployée et
re-vérifiée (`.env` + variable d'environnement du conteneur confirmées à
`false`, 402 toujours correct sur `/search`).

À refaire quand le premier vrai règlement sera encaissé et l'indexation
Bazaar confirmée : `FREE_FIRST_CALL=true` dans `.env` du VPS, rebuild
(`docker compose build && docker compose up -d`), remettre « first call free
per wallet » dans `ROUTE_DESCRIPTIONS`.

## Découvrabilité AgentCash (recherche sémantique) - 2026-09-05

Conforme au validateur `discover`/`check` (0 avertissement) mais absent de
`npx agentcash search "web search"` / `"translate text"` / `"batch translate
segments"` / `"research brief"` - le classement est une similarité vectorielle
sur un `semanticDescription` généré par leur backend, structuré en lignes
`Keyword:`/`Use case:`.

**Hypothèse initiale invalidée par la preuve, changement de plan en cours de
route** : la demande de départ supposait que `Keyword:`/`Use case:` venaient
d'un champ `tags`/`x-use-cases` qu'on nous demandait de remplir. Vérifié en
récupérant l'`openapi.json` réel d'un concurrent classé (`4yearcycle.com`,
requête `agentcash search "web search"`, résultat `/x402/web-search`) : son
opération n'a **ni `tags` ni aucun champ `x-use-cases`** - seulement
`summary`, `description`, `parameters` (chacun avec une vraie `description` et
souvent un `example`), et un schéma de réponse `200` entièrement décrit
propriété par propriété. Les 6 `Keyword:` et 8 `Use case:` de son
`semanticDescription` ne sont des copies verbatim d'aucun champ brut de son
spec (ex. « domain filter » vient du nom+description des paramètres
`include_domains`/`exclude_domains`, « synthesized answer » de la description
du champ de réponse `answer`) - donc synthétisés par leur indexeur (LLM) à
partir de tout le texte descriptif disponible, pas lus depuis un champ dédié.

**Conséquence** : le vrai levier n'est pas un champ magique à remplir, c'est
la densité de texte descriptif réel dans le schéma. Ajouté par route payante
(`app/x402_setup.py`, dérivé pour l'OpenAPI dans `app/openapi_custom.py`,
zéro valeur dupliquée) :
- `summary` : une phrase de résultat, distincte de la `description` existante
  (laissée intacte, sur demande explicite).
- Une `description` sur **chaque propriété** du `requestBody`
  (`SEARCH_INPUT_SCHEMA`, `TRANSLATE_INPUT_SCHEMA`, `JOBS_INPUT_SCHEMA` -
  déjà la source unique pour Bazaar, MCP et l'OpenAPI, maintenant enrichie
  pour les trois d'un coup).
- Un vrai schéma de réponse `200` entièrement décrit (`SEARCH_OUTPUT_SCHEMA`,
  `TRANSLATE_OUTPUT_SCHEMA`, `JOBS_OUTPUT_SCHEMA` - avant : `{}` vide, FastAPI
  n'ayant jamais eu de `response_model` puisque les handlers renvoient des
  dicts bruts), branché aussi dans `OutputConfig(schema=...)` côté Bazaar.
- `tags` (5-8 mots-clés par route, ex. `/search` : web search, live search,
  SERP, ranked results, multi-query, deduplicated, page extract, current
  news) et `x-use-cases` (8 phrases à l'impératif par route) tout de même
  ajoutés - champ standard OpenAPI pour l'un, pari à coût nul pour l'autre, au
  cas où leur pipeline scannerait le corps brut de l'opération au-delà des
  champs qu'on a pu confirmer.

**Vérifié** : `openapi.json` toujours valide (`openapi_spec_validator`),
`npx -y @agentcash/discovery@latest discover` toujours à 0 avertissement en
prod après déploiement, `npx agentcash check .../search` confirme le nouveau
`summary`/`inputSchema`/`outputSchema` bien lus en direct depuis notre spec.

**Pas encore vérifiable** : `npx agentcash register` relancé avec succès
(`registered: 1` sur x402scan) mais `npx agentcash search` ne nous fait
toujours apparaître sur aucune des 4 requêtes cibles, même sur une requête
construite avec notre propre vocabulaire exact - l'indexation
sémantique/vectorielle est manifestement asynchrone côté AgentCash (le
`register` déclenche un crawl, pas un recalcul d'embedding immédiat) et rien
côté CLI ne permet d'en observer l'avancement. À revérifier plus tard
(délai inconnu) : `npx agentcash search "web search"` /
`"batch translate"` / `"research brief"`, et lire le `semanticDescription`
retourné pour nous une fois indexés - c'est la seule vraie confirmation que
leur backend a compris l'un des ajouts ci-dessus.

## Bug réel critique : `/search` et `/translate` renvoyaient 502 aux paiements réels (2026-09-05)

Trouvé en diagnostiquant `x402scan: registered 1, skipped 2` sur `npx
agentcash register`. Le CLI `agentcash` (`npm pack agentcash`) ne fait que
poster vers `x402scan.com` et parser sa réponse - aucune logique de décision
côté client. Cloné leur dépôt public (`Merit-Systems/x402scan` sur GitHub,
`apps/scan/src/lib/discovery/register-origin.ts` et `probe.ts`) pour lire le
vrai code serveur : chaque ressource payante est sondée en direct (`GET`,
`POST`, `PUT`, `DELETE`, `PATCH`, sans corps) via `@agentcash/discovery`'s
`checkEndpointSchema`, et un endpoint sans réponse "utilisable" (402 ou 2xx)
sur aucune méthode est classé `skipped`.

Rejoué exactement cette sonde en local (`checkEndpointSchema` importé
directement depuis le paquet mis en cache par `npx`) contre nos 3 routes :
`/jobs` répondait bien, `/search` et `/translate` répondaient... **502 Bad
Gateway**. Confirmé au `curl` brut : `POST /search` et `POST /translate`
étaient cassés pour TOUT appelant depuis le déploiement précédent (celui qui
a enrichi le schéma OpenAPI pour la recherche sémantique AgentCash), pas
seulement pour le crawler de x402scan - **un vrai bug de paiement en
production**, pas un simple souci de découvrabilité.

Cause : `proxy_pass` derrière nginx a des buffers de réponse par défaut trop
petits (souvent 4 Ko) pour l'en-tête `payment-required`, qui embarque tout le
challenge x402 base64 **plus** l'extension Bazaar (tags, x-use-cases, schémas
de sortie) qu'on vient d'enrichir. Mesuré en direct sur le port interne
(bypass nginx) : en-tête `payment-required` de `/search` = 4312 octets,
`/translate` similaire, `/jobs` = 2592 octets (plus petit, sous le seuil,
d'où le seul `registered:1` avant correctif). Nginx tronque/rejette l'en-tête
trop gros et renvoie 502 au lieu de relayer le vrai 402 - exactement le
symptôme documenté par x402scan lui-même dans `probe.ts` ("Some merchants
embed the full 402 payment body in a `payment-required` header, exceeding
Node's default 16 KB limit") mais côté nginx, pas côté client.

Corrigé dans le vhost nginx dédié (`/etc/nginx/sites-available/
x402.agentindex.world.conf`, jamais le nginx global - sauvegarde horodatée
faite avant modification) : `proxy_buffer_size 32k; proxy_buffers 8 32k;
proxy_busy_buffers_size 64k;` sur le bloc `location /`. `nginx -t` validé
avant `systemctl reload nginx`.

**Vérifié** : les 3 méthodes/routes retestées en `curl` brut donnent 402
partout après rechargement. Sonde `checkEndpointSchema` rejouée : `found:
true` sur les 3. `npx agentcash register` : `registered: 3, siwx: 0, failed:
0, skipped: 0` - objectif atteint.

**Leçon** : chaque enrichissement de l'extension Bazaar/OpenAPI (tags,
x-use-cases, schémas de sortie détaillés) fait grossir l'en-tête
`payment-required` de TOUTES les routes qui le portent. Si de futurs ajouts
repoussent `/jobs` (ou une nouvelle route) au-delà de 32 Ko, ce sera le même
symptôme - vérifier la taille réelle de l'en-tête (`curl -s -D - -o /dev/null
-X POST <route> | grep -i payment-required | wc -c`) après tout changement
touchant `build_route_configs()` ou `openapi_custom.py`.

## MPP implémenté en parallèle de x402 (2026-09-05) - décision du matin inversée

Raison de l'utilisateur : MPP avait été écarté faute de paiement MPP observé,
mais c'est une condition d'entrée dans l'index mppscan, pas un rail de
paiement dont on attend du volume - la bonne mesure est l'inscription et la
recherche, pas le trafic.

**Méthode choisie : `evm` / intent `charge` / credential `authorization`**
(specs/methods/evm/draft-evm-charge-00.md, tempoxyz/mpp-specs) - c'est très
exactement EIP-3009 `transferWithAuthorization`, le même mécanisme déjà
utilisé par le schéma "exact" de x402 pour l'USDC sur Base. La vérification
de signature est donc de la pure cryptographie, aucune nouvelle hypothèse de
confiance introduite.

**Nouveau module `app/mpp.py`** : construction du challenge
`WWW-Authenticate: Payment ...` (liaison HMAC-SHA256 exactement comme
recommandé par `draft-httpauth-payment-00`, JCS simplifié suffisant pour nos
payloads plats sans flottant), vérification complète d'un credential soumis
(recouvrement de signature EIP-712, liaison au challenge via
`nonce = keccak256(id + realm)`, expiration, montant, destinataire),
règlement on-chain via un compte serveur **géré par CDP**
(`cdp.evm.get_or_create_account` + `send_transaction`) - jamais de clé privée
brute sur ce serveur, cohérent avec le reste du projet.

**Nouvelle middleware `app/mpp_middleware.py`**, ajoutée à l'extérieur du
flux x402 existant, jamais dedans :
- Requête normale (pas de credential MPP) : passe intacte jusqu'à x402 ; si
  la réponse est un 402, un second en-tête `WWW-Authenticate: Payment` est
  simplement ajouté à côté du `payment-required` de x402 (pattern "Multiple
  Payment Options" du spec) - le corps et les en-têtes x402 ne sont jamais
  touchés.
- Requête avec `Authorization: Payment <credential>` : interceptée avant
  x402/IntentLoggingMiddleware, vérifiée et réglée ici: en cas de succès,
  `inner_app` est appelé directement (même handler que x402 aurait appelé -
  la logique métier ne bifurque jamais entre les deux rails), en cas d'échec
  un 402 frais est renvoyé sans jamais modifier d'état (`seen`/facturation).

**Anti-rejeu réfléchi** : le nonce n'est marqué consommé qu'**après** un
règlement on-chain réussi, jamais avant. Bug évité pendant le test local :
marquer le nonce consommé dès la vérification aurait définitivement brûlé
l'autorisation d'un payeur légitime à la première tentative de règlement
ratée (ex. compte de règlement pas encore approvisionné) - le nonce EIP-3009
est de toute façon appliqué on-chain par le contrat du token lui-même, notre
table `mpp_nonces` n'est qu'un raccourci pour éviter de retenter une requête
déjà réussie, pas la seule garantie anti-double-paiement.

**Vérifié avec un signataire de test réel** (`eth_account`, script jetable) :
signature EIP-712 recouvrée correctement, falsification de montant/valeur
détectée et rejetée, `WWW-Authenticate` bien présent à côté de
`payment-required` sur les 3 routes en local ET en prod, taille totale des
en-têtes toujours large sous les 32 Ko de buffer nginx corrigés plus haut.

**Vérifié avec leurs outils officiels** : `npx -y agentcash@latest register
"https://x402.agentindex.world"` donne maintenant `x402scan: registered 3,
skipped 0` ET `mppscan: registered 13, failed 0` (pas de champ littéral
`success: true` dans leur réponse contrairement à ce qu'on attendait au
départ - leur schéma n'en définit pas ; `type: "done"` + `failed: 0` +
`failedDetails: []` est leur signal de succès réel). `discover` reste à 0
avertissement, les 3 routes payantes affichent maintenant `[x402, mpp]`.

**Pas encore vérifiable, comme pour AgentCash** : `agentcash search "web
search"` / `"batch translate"` / `"research brief"` ne nous montrent toujours
pas juste après l'inscription - l'indexation sémantique est asynchrone côté
AgentCash (déjà observé sur x402scan), à revérifier plus tard.

**Écart réel restant, hors de ma portée** : le règlement on-chain réel
échoue actuellement avec `"Wallet Secret not configured"` - `CDP_WALLET_SECRET`
(distinct de `CDP_API_KEY_ID`/`CDP_API_KEY_SECRET` déjà configurés) n'est pas
défini, et même une fois défini, le compte serveur CDP dédié
(`MPP_SETTLEMENT_ACCOUNT_NAME`, défaut `"mpp-settlement"`) doit être
approvisionné en un peu d'ETH sur Base pour payer le gas des règlements - une
action réelle avec de l'argent que je ne peux pas faire moi-même. Tant que ce
n'est pas fait, le challenge/la vérification fonctionnent entièrement (donc
l'inscription mppscan et la recherche sémantique, l'objectif de cette tâche),
mais un vrai payeur MPP recevrait un 402 propre au lieu d'un règlement réussi
- pas un risque de sécurité (aucun faux succès n'est jamais renvoyé), juste
une fonctionnalité incomplète tant que le compte n'est pas financé.

## Usine à endpoints - chantier d'infrastructure (2026-09-05)

Brief : boucle autonome qui trouve des intentions mal servies, fabrique des
routes, les valide, les inscrit, mesure ce qui encaisse, et tue ce qui ne
rapporte rien. Deux découvertes en explorant avant de construire, qui ont
changé la forme du chantier - voir le plan complet dans l'historique de
session (`enchanted-dancing-dream.md`) :

1. Le dépôt `comallagency/agents` (Surveillant/Réparateur/Chercheur/Rédacteur)
   cité comme "en service" dans le brief est en fait **gelé** depuis le
   28/08 (arrêt complet demandé par l'opérateur). L'usine x402 est un
   chantier totalement indépendant.
2. `chain_payments.py` n'a pas de champ route (tous les payTo sont
   identiques) - l'attribution par route se fait en corrélant chaque
   transfert on-chain avec nos propres lignes `requests` (payeur + montant +
   plus proche dans le temps, fenêtre de 5 minutes) - voir
   `db.route_chain_stats()`.

**Répartition confirmée par l'utilisateur** : Contrôleur/Comptable/Fossoyeur
mécaniques (Python pur, zéro appel de modèle, VPS, cron, PC éteint) ;
Prospecteur/Ouvrier/Crieur jugement (session Claude Code lancée à la main sur
le PC, aucun compte Anthropic sur le serveur).

**Registre de routes piloté par config** (`app/generated/`) : `registry.py`
(dataclass `RouteSpec`, YAML), `proxy_handler.py` (handler générique
`http_proxy` - un seul pattern réutilisable, sur le modèle agentutility,
817 routes avec quelques patterns génériques plutôt que 817 fichiers),
`dynamic_routes.py` (agrège dans `build_route_configs()`,
`capacity.ROUTE_KEYS`, les tools MCP et les entrées OpenAPI - jamais une
deuxième source de vérité). Vérifié en local avec une route jouet réelle
(upstream httpbin.org) : apparaît dans l'OpenAPI, le 402 se déclenche, `/sample`
appelle vraiment l'upstream, le tool MCP est enregistré et correctement gaté
par paiement - puis retirée, le registre reste vide (aucune route de
démonstration ne devait sortir de ce chantier lui-même).

**Bug réel trouvé pendant le développement du tool MCP générique** :
`fastmcp`'s `Tool.parameters` peut être écrasé après coup pour changer ce que
`tools/list` annonce, mais **la validation d'appel réelle se fait contre la
signature Python de la fonction, pas contre `.parameters`** - confirmé
empiriquement (un override menteur passe le listing mais casse l'appel
réel). Corrigé en enveloppant l'entrée sous une clé `payload` unique dont le
schéma annoncé est honnête (`{"type":"object","properties":{"payload":
<vrai schéma>}}`), plutôt que de synthétiser une signature Python par route.

**CDP `/platform/v2/x402/validate`** : le SDK Python (`cdp-sdk`) a un modèle
de réponse strict qui a déjà pris du retard sur l'API réelle (un check
`"url_valid"` absent de son enum fait planter le parse pydantic). Contourné
en appelant `validate_x402_resource_without_preload_content` et en
parsant le JSON brut - confirmé avec un vrai appel contre `/search` en
production (`valid: true`, `simulation.outcome: "accepted"`).

**Bug réel trouvé au premier déploiement (avant même le cron)** : les
scripts mécaniques dans `scripts/` (sous-dossier) plantaient avec
`ModuleNotFoundError: No module named 'app'` lancés comme
`python scripts/controleur.py` - contrairement à `chain_payments.py` (racine
du dépôt), le dossier `scripts/` n'est jamais sur `sys.path` de cette façon.
Corrigé : `scripts/__init__.py` ajouté, invocation systématique via
`python -m scripts.controleur` (le cwd, `/app`, atterrit sur `sys.path`, pas
le dossier du fichier). Toutes les entrées cron mises à jour en conséquence.

**Fossoyeur tourne sur l'hôte, pas dans le conteneur** - décision
d'isolation, pas une contrainte technique : retirer une route implique un
rebuild Docker, et faire ça depuis l'intérieur d'un conteneur nécessiterait
de monter le socket Docker de l'hôte dedans, donnant à ce conteneur un
contrôle total sur TOUT Docker de l'hôte (hermes/myclawio compris) -
inacceptable sur ce VPS partagé. Un venv dédié minimal a été créé
(`/opt/x402/fossoyeur-venv`, juste `pyyaml`), et `fossoyeur.py` n'importe
jamais le paquet `app/` (FastAPI/x402/cdp-sdk) - juste `sqlite3` (stdlib) et
`yaml`, pour rester léger sur l'hôte partagé.

**Vérifié en réel, pas en théorie** : `controleur.py` et `comptable.py`
lancés pour de vrai dans le conteneur de production
(`docker compose run --rm x402 python -m scripts.<nom>`) - 3/3 routes
vertes, chain sync réel exécuté. `fossoyeur.py` lancé pour de vrai sur
l'hôte avec le nouveau venv - `aucune route à retirer` (registre vide,
comportement attendu). `catalogue_sync.py` a réellement téléchargé les
16 540 ressources du Bazaar CDP en local et produit un instantané exploitable
(comptage par mot-clé cohérent : "price" 2033 mentions, "translate" 37 -
confirme empiriquement que la traduction est un terrain moins disputé que la
recherche, utile pour Prospecteur). `/admin/usine` et `/admin/usine.json`
vérifiés en production avec de vraies données (règlements tentés, 3 routes
vertes, agents avec horodatage réel). Node.js/npm ajoutés uniquement à
l'image Docker x402 (jamais à l'hôte partagé) - testé dans un build Docker
local complet avant tout déploiement.

**État au 2026-09-05** : infrastructure en place et vérifiée, registre vide
par construction - aucune route de démonstration n'a été fabriquée par ce
chantier (voir `usine/README.md`). La prochaine étape est une vraie session
Prospecteur/Ouvrier/Crieur (`usine/{prospecteur,ouvrier,crieur}.md`), lancée
à la main, partant d'une intention réelle trouvée dans
`data/catalogue_snapshot.json`. Cron VPS installé sous le user `x402`
(sauvegarde de l'ancien crontab faite avant remplacement) ; `chain_payments.py`
en cron horaire remplacé par `comptable.py` qui fait le même sync puis calcule
en plus la répartition par route.

## Boutons /admin/usine (2026-09-05) et point d'arrêt Prospecteur→Ouvrier (2026-09-06)

**`routes_registry.yaml` est baké dans l'image Docker, pas bind-monté** :
seuls `data/` et `logs/` le sont (voir `docker-compose.yml`). Un endpoint
FastAPI servi par le conteneur en cours d'exécution ne peut donc PAS
persister une écriture dans ce fichier - exactement la contrainte qui impose
à Fossoyeur de tourner hors conteneur, étendue ici : le workflow de
proposition/approbation (Prospecteur propose, l'opérateur approuve depuis le
dashboard, Ouvrier construit) vit dans une table SQLite `proposals`
(`data/requests.db`, bind-monté, persiste) plutôt que dans le YAML.

**Bug bash réel trouvé en testant `usine/nightly_run.sh`** : `local role="$1"
outfile="$OUT_DIR/claude_${role}.json"` échoue sous `set -u` avec `role:
unbound variable` - bash évalue TOUTES les valeurs d'un `local` multi-
variables dans l'environnement AVANT de déclarer les variables, donc
`${role}` référencé dans la valeur de `outfile` sur la MÊME ligne `local`
n'existe pas encore. Corrigé en séparant en deux instructions `local`
successives. Trouvé en lançant le script pour de vrai avec un faux binaire
`claude` (`PATH` shadowing) plutôt qu'en le relisant - `bash -n` ne détecte
pas ce genre d'erreur (elle n'est qu'à l'exécution).

**Vérifié en réel** : cycle complet propose→approve→close testé contre un
serveur local (transitions de statut correctes, 409 sur double-approbation,
404 sur id inconnu) puis contre la production (`/admin/usine.json` expose
`proposals`, JS du dashboard toujours syntaxiquement valide après ajout de
la table Propositions). `usine/nightly_run.sh` testé avec un faux `claude`
shadowé via `PATH` dans les deux branches (aucune proposition approuvée ->
Ouvrier/Crieur sautés ; une proposition approuvée -> Ouvrier puis Crieur
enchaînés ; échec d'Ouvrier -> Crieur sauté) - jamais de vraie session
`claude --print` autonome lancée pendant les tests, pour ne pas risquer un
vrai déploiement pendant une vérification.

## Activation réelle + visibilité PC sur le dashboard (2026-09-06)

**Tâche planifiée enregistrée** (feu vert explicite de l'opérateur) :
`Usine-x402`, 05:00 quotidien, `Get-ScheduledTask` confirme l'état `Ready`.
**Raccourci Bureau** ajouté (`usine/run_nightly_now.bat` + `.lnk`) pour un
lancement à la demande, fenêtre cmd visible (`chcp 65001` pour les accents
français, `pause` en fin pour laisser le résultat affiché).

**Le dashboard ne pouvait rien dire du pipeline PC avant cette session** -
corrigé avec deux nouveaux endpoints (`POST /admin/usine/pc/role_run`,
`POST /admin/usine/pc/status`) que `nightly_run.sh` appelle lui-même : par
rôle (réutilise `journal(action='run')`, donc les cartes agent
Prospecteur/Ouvrier/Crieur affichent enfin un vrai `last_run` au lieu de
"jamais lancé" figé) et pour l'ensemble du run (nouvelle table
`pc_pipeline_status`, une ligne unique, avec le résultat global et l'état de
la tâche planifiée). Corps JSON construit par `python3`/`json.dumps` dans le
script, jamais interpolé à la main dans un `-d` curl - `detail` peut
contenir des guillemets, un JSON assemblé par concaténation shell serait
invalide ou pire.

**État de la tâche planifiée détecté via `Get-ScheduledTask` (PowerShell),
jamais `schtasks.exe`** : ce dernier ne renvoie que du texte localisé selon
la langue de Windows ("Prêt"/"Ready" selon la machine), invérifiable de
façon fiable. `(Get-ScheduledTask -TaskName ...).State` renvoie une valeur
d'énumération stable (`Ready`/`Running`/`Disabled`) quelle que soit la
langue - appelé depuis WSL via l'interop `powershell.exe`, déjà vérifié
fonctionnel dans cet environnement.

**Vérifié en réel** avant d'activer la tâche : cycle complet
role_run/status testé contre un serveur local (les deux endpoints
persistent bien, la carte Prospecteur affiche le vrai résumé et le vrai
`last_run`) ; `nightly_run.sh` re-testé de bout en bout avec le faux
`claude` (mêmes deux branches qu'avant + vérification que les deux rapports
partent bien et sont lisibles dans `/admin/usine.json`) ; interop
`powershell.exe` depuis WSL confirmée fonctionnelle avant d'en dépendre dans
le script ; après l'enregistrement réel de la tâche, la requête
`Get-ScheduledTask` de `nightly_run.sh` a été rejouée à la main et renvoie
bien `Ready` - le prochain vrai run rapportera `task_active: true`, pas
`null`.

## Inversion de contrôle PC <-> VPS (2026-09-06)

**Le VPS ne peut jamais lancer un programme sur le PC de l'opérateur** -
demande explicite de l'utilisateur de remplacer le raccourci Bureau comme
point d'entrée principal par un bouton `/admin/usine`. Le sens du contrôle
s'inverse : `usine/pc_listener.py` sonde `GET /admin/usine/pc-request`
toutes les 30s depuis le PC : c'est lui qui appelle le VPS, jamais l'inverse.

**Authentification machine, pas Basic Auth** : `GET /admin/usine/pc-request`
et `POST /admin/usine/pc-request/{id}/status` sont protégés par un jeton
dédié (`PC_LISTENER_TOKEN`, header `X-Pc-Token`, `secrets.compare_digest`) -
un écouteur qui tourne seul sur le PC n'a pas de session navigateur avec
Basic Auth en cache. Jeton généré une fois (`secrets.token_urlsafe(32)`),
identique des deux côtés : `.env` du VPS et
`~/.config/agentindex-x402/pc_listener_token` côté PC - **volontairement
HORS du dépôt** (pas dans `.env.production`, jamais committé), exigence
explicite de l'utilisateur au-delà du simple `.gitignore`.

**Un seul verrou global côté serveur** (`db.latest_pc_request()`), pas un
verrou par rôle : pipeline et les 3 rôles de jugement tournent tous dans la
même session PC et ne doivent jamais se chevaucher (Ouvrier et Crieur en
particulier). Contrairement aux 3 boutons mécaniques
(`agent_manual_runs`, verrou par rôle car ils tournent réellement en
parallèle dans des conteneurs/hôte séparés), un seul `pc_requests` actif à
la fois, tous rôles confondus.

**Le battement de coeur est un effet de bord du sondage, pas un endpoint
séparé** : chaque appel réussi à `GET /admin/usine/pc-request` (qu'il y ait
une demande en attente ou non) met à jour `pc_listener_heartbeat`. Le
dashboard affiche "connecté" si le dernier contact date de moins de 90s (3x
le cycle de 30s) - au-delà, "non connecté" avec l'horodatage du dernier
contact réel, jamais une supposition.

**Fenêtre cachée : un VBScript intermédiaire, pas les réglages du
Planificateur** - `Get-ScheduledTask`/`Register-ScheduledTask` proposent des
options "masqué" mais elles ne suppriment pas fiablement la fenêtre console
d'un programme comme `wsl.exe` lancé en tâche planifiée. Le contournement
standard : la tâche appelle `wscript.exe` sur un `.vbs`
(`usine/pc_listener_hidden.vbs`) qui lui-même appelle
`WshShell.Run(cmd, 0, False)` - le `0` masque réellement la fenêtre.

**Trois chemins de déclenchement, un seul verrou `flock`** : la tâche de
05:00, le raccourci Bureau et l'écouteur invoquent tous `nightly_run.sh`
indépendamment, sans se connaître. Un verrou non-bloquant
(`exec 9>"$LOCK_FILE"; flock -n 9`) en tête de script évite que deux
d'entre eux déploient en SSH en même temps si jamais ils se chevauchent
(ex. clic sur le raccourci pendant que l'écouteur traite une demande du
dashboard) - celui qui trouve le verrou pris s'arrête immédiatement au lieu
d'attendre ou de risquer un conflit sur `routes_registry.yaml`/le build
Docker.

**`nightly_run.sh --role <role>` réutilise le même point d'arrêt et le même
chaînage que le pipeline complet** - factorisé dans
`run_ouvrier_and_crieur_if_approved()` : `--role ouvrier` vérifie les
propositions approuvées exactement comme le pipeline complet (jamais de
contournement selon le bouton qui a déclenché le run) et enchaîne Crieur
automatiquement après, comme le pipeline complet.

**Vérifié en réel** : cycle complet propose/approve puis clic
pipeline/rôle testé contre un serveur local avec un faux `claude` (shadowé
via `PATH`) ET un faux `nightly_run.sh` (source de `.env.production`
neutralisée, variables pré-exportées vers `127.0.0.1`) - la demande passe
bien de `requested` à `running` à `done`, le battement de coeur se met à
jour, le verrou global rejette une seconde demande avec 409. Jeton testé
dans les deux sens (bon jeton -> 200, mauvais/absent -> 401) contre le
serveur local ET contre la production après déploiement. Régression
vérifiée : les 3 boutons mécaniques (fusionnés dans le même endpoint
`POST /admin/usine/run/{role}`, Literal élargi) fonctionnent toujours
(testé `comptable` après le changement).

**Activé le 2026-09-06** (feu vert donné). Trois bugs réels trouvés en le
lançant pour de vrai contre la production - aucun n'était visible en
relisant le code, tous visibles uniquement en observant le tableau de bord
en désaccord avec ce qui semblait tourner :

**Bug 1 - `--add-dir` avale le prompt.** `claude --print ... --add-dir
"$REPO" "$prompt"` échoue systématiquement avec "Input must be provided
either through stdin or as a prompt argument when using --print", quel que
soit le contenu du prompt. Cause : `--add-dir <directories...>` est
variadique (accepte plusieurs valeurs) et engloutit le prompt positionnel
suivant comme un répertoire supplémentaire au lieu de le laisser à `claude`
comme argument. Le shell ne signale aucune erreur de syntaxe - `claude`
reçoit juste zéro argument positionnel et se plaint, dans un message qui ne
mentionne ni `--add-dir` ni un problème d'arguments. Reproduit isolément
(`claude --add-dir X --print Y "prompt"` échoue, `claude --add-dir X --
"prompt"` réussit) avant de conclure. Fix : `--` avant le prompt dans
`run_role()` de `usine/nightly_run.sh` - sépare définitivement les options
du positionnel, quel que soit leur ordre.

**Bug 2 - le script ne rapportait jamais un échec.** `report_final_status()`
se terminait sur `report_pipeline_status(...)`, un appel réseau qui réussit
même quand le rôle a échoué - donc le code de sortie du script était
toujours 0, et `usine/pc_listener.py` marquait systématiquement la demande
"done" même après un Prospecteur planté (observé en réel : le journal disait
"ERREUR prospecteur : code shell 1" mais `pc_requests.status` affichait
`done`). Fix : `report_final_status()` se termine sur `[ "$overall_result" =
"ok" ]`, dont le code de sortie devient celui du script entier.

**Bug 3 - le sondage se bloquait pendant toute la durée d'une session**
(le plus important des trois, signalé par l'opérateur qui a comparé le
tableau de bord à ce que l'agent racontait, pas l'inverse). La première
version de `poll_once()` appelait `subprocess.run()` directement dans la
boucle principale - donc le sondage suivant (et le battement de coeur qu'il
produit) n'arrivait qu'APRÈS que `nightly_run.sh` se termine. Une session
Prospecteur de quelques minutes suffisait à faire apparaître l'écouteur
"non connecté" sur `/admin/usine`, alors que le processus tournait
réellement (confirmé par `ps`/`pstree` - le process n'était pas mort, juste
bloqué). Fix : `usine/pc_listener.py` lance `nightly_run.sh` dans un
`threading.Thread` séparé ; la boucle principale continue de sonder
(donc de battre le coeur) toutes les 30s indépendamment de la durée de la
session en cours, et ne relance pas une deuxième tâche tant que le thread
actif n'est pas terminé.

**Garde-fou ajouté à la demande de l'opérateur** : une `pc_requests` restée
à `requested`/`running` plus de 20 minutes (PC éteint, écouteur arrêté,
session qui plante sans jamais rappeler) passe automatiquement à `timeout`
à la LECTURE (`db.latest_pc_request()`, appelé aussi bien pour l'affichage
que pour le verrou de `POST /admin/usine/run/{role}`) - le bouton redevient
cliquable sans attendre un redémarrage de qui que ce soit, et le passage est
journalisé (`operator, pc_timeout`).

**Vérifié en réel, dans cet ordre** : (1) l'erreur "Input must be
provided..." reproduite isolément et corrigée avant de toucher au script
complet ; (2) le timeout de 20 min testé en rembobinant `requested_at` dans
la base locale plutôt qu'en attendant 20 minutes pour de vrai, transition
et journalisation confirmées, un nouveau clic accepté ensuite ; (3) le
sondage non-bloquant testé avec un faux `claude` qui dort 70s - le
battement de coeur (`/admin/usine.json` -> `pc_listener.connected`) reste
`true` en continu pendant toute la durée, jamais observé avec l'ancienne
version ; (4) après déploiement, le process bloqué de la session réelle en
cours a été arrêté proprement (`kill` sur le groupe de processus, la
demande PC associée expirera via le nouveau garde-fou plutôt que d'être
relancée), l'écouteur redémarré, et `pc_listener.connected: true` confirmé
en interrogeant `https://x402.agentindex.world/admin/usine.json` pour de
vrai - jamais seulement `ps`/`pstree` sur le PC.

## Boucle autonome complète, sans point d'arrêt (2026-09-06)

Demande de l'opérateur : "j'appuie sur Lancer, le système travaille et
déploie sans jamais me demander quoi que ce soit, jusqu'à ce que j'appuie
sur Arrêter". Approbation des propositions rendue automatique (traçabilité
gardée : `proposed` -> `auto_approved`, distinct d'un `approved` humain).
Nouvel orchestrateur `usine/loop_run.sh` : cycle Prospecteur -> Ouvrier ->
Contrôleur (passage COMPLET, toutes les routes, pas seulement la neuve) ->
Crieur -> Comptable -> Fossoyeur, en boucle.

**Refactor `usine/lib.sh`** : la logique d'invocation `claude`
(`run_role`, `claude_ok`, `report_role_run`, etc.) était dupliquée entre
`nightly_run.sh` et le nouveau `loop_run.sh` - extraite dans une
bibliothèque partagée, sourcée par les deux. Le bug `--add-dir` du même
jour aurait dû être corrigé à un seul endroit dès le début ; cette
extraction évite que ça se reproduise. `nightly_run.sh` re-testé (mêmes
scénarios qu'avant le refactor) pour confirmer que rien n'a cassé.

**Réparation autonome** (`usine/reparateur.md`) : une route DÉJÀ EN LIGNE
qui régresse déclenche une session ciblée, puis le Contrôleur REVÉRIFIE
lui-même (l'agent ne s'auto-évalue jamais) via `POST
/admin/usine/loop/repair_attempt` - compteur consécutif remis à zéro dès
qu'une revérification passe, 2 échecs d'affilée -> retrait MÉCANIQUE
(`loop_run.sh::kill_route_mechanically`, édition directe de
`routes_registry.yaml` + rebuild, jamais une décision d'agent).

**Sentinelle mécanique** (`usine/sentinel_check.py`), zéro appel de modèle,
rejouée avant CHAQUE cycle : hash SHA256 des fichiers critiques du paiement
(baseline commitée dans `usine/sentinel_baseline.json`, établie en
interrogeant la vraie prod le jour même), `X402_PAY_TO` inchangé,
conteneurs hermes (`myhermes-u278/u194/u198`) toujours "Up", isolation des
dossiers `/home/hermes`/`/home/myclawio` (propriétaire + permissions 750)
intacte. **myclawio n'a actuellement aucun conteneur en service** - vérifié
en interrogeant `docker ps -a` avant d'écrire la baseline, pas supposé ;
la sentinelle vérifie donc son isolation de dossier mais pas de conteneur
pour ce user-là. Testée dans les deux sens (référence correcte ET
référence sabotée exprès avec un mauvais payTo/conteneur inventé) avant
déploiement, contre la vraie prod (lecture seule, sans risque) :
`sys.exit(1)` confirmé effectif dans les deux cas après avoir d'abord cru
le contraire à cause d'un artefact de test (capture `$?` invalide dans une
commande bash inline enchaînée - `false; echo $?` renvoyait 0 dans ce
contexte précis ; refait via un fichier de script exécuté séparément,
seule méthode fiable établie tout au long de cette session pour ce genre
de vérification).

**Arrêts automatiques** centralisés côté serveur, jamais dupliqués en bash :
`app/admin.py::LOOP_MAX_CONSECUTIVE_REJECTIONS=3`,
`REPAIR_MAX_ATTEMPTS=2`, `config.OPENROUTER_DAILY_BUDGET_USD=2.00`. Point
d'attention réel sur ce dernier : la clé OpenRouter du projet n'a *aucun*
`limit` configuré côté OpenRouter (`limit`/`limit_remaining` valent `null`
sur `/api/v1/key`, vérifié en interrogeant la clé réelle) - il n'existe
donc aucun signal natif de "quota restant en %" à lire. Le "budget
journalier sous 10%" implémenté ici compare `usage_daily` (dollars
dépensés) à un seuil propre au projet ($2, reprend l'objectif de départ),
PAS le quota de requêtes gratuites par modèle qu'OpenRouter applique
réellement aux appels `:free` des routes `/search`/`/translate` - cette
distinction n'a pas été validée avec l'opérateur, à corriger si ce n'est
pas ce qu'il avait en tête.

**Notification Windows** (bulle système `NotifyIcon`, pas l'API Toast
moderne - celle-ci exige souvent un AUMID d'application enregistrée pour
s'afficher depuis un script PowerShell ad hoc, la bulle système fonctionne
de façon fiable sans rien enregistrer). Bug réel trouvé en testant
l'appel pour de vrai (pas en le relisant) : `subprocess.run([...],
text=True)` plantait avec `UnicodeDecodeError` sur la sortie de
`powershell.exe`, qui n'est pas garantie en UTF-8 - fixé en capturant les
bytes bruts et en décodant avec `errors='replace'` seulement pour le
message de log en cas d'échec.

**Vérifié en réel avant déploiement** : cycle complet testé en local avec
faux `claude` et fausse fonction `ssh_vps` (deux scénarios - route
neuve acceptée et route neuve rejetée - registre restauré après chaque
test), `kill_route_mechanically` testé isolément sur un registre à deux
routes (une retirée, l'autre intacte), sentinelle testée dans les deux
sens contre la vraie prod. **La boucle n'a volontairement PAS été
démarrée** - construite, déployée, écouteur redémarré avec le nouveau code
et confirmé connecté (`pc_listener.connected: true` via
`/admin/usine.json`), mais le premier `loop_start` réel est le geste de
l'opérateur, pas un geste à automatiser depuis cette session.
