# Ouvrier

Fabrique une route à partir d'une intention retenue par Prospecteur. Tout
dérivé de `app/generated/registry.py::RouteSpec` - jamais de valeur en dur
dupliquée ailleurs (c'est exactement le bug de dérive des tags déjà
rencontré, voir BRIEF-CORRECTIONS.md).

## Étapes

1. **Choisir `handler_type`** : `http_proxy` (générique, `app/generated/
   proxy_handler.py`) couvre la quasi-totalité des cas - appeler un
   upstream, reshaper, facturer. N'écrire un handler `custom` que si le
   générique ne suffit vraiment pas.

2. **Écrire l'entrée de registre** dans `app/generated/routes_registry.yaml` :
   - `slug` : court, en anglais, pas d'espace.
   - `price` : cohérent avec la valeur réelle rendue (pas juste "comme les
     autres").
   - `description` : ce que l'acheteur reçoit, jamais le nom du fournisseur
     amont, jamais le mécanisme d'appel exact (même règle que
     search/translate/jobs).
   - `tags` (5-8) et `use_cases` (8 phrases à l'impératif, "search the...",
     "verify a...") : voir `app/x402_setup.py::ROUTE_SUMMARIES`/
     `ROUTE_USE_CASES` pour le ton exact déjà validé.
   - `input_schema`/`output_schema` : JSON Schema réel, avec une
     `description` sur chaque propriété - c'est la matière que les
     catalogues utilisent pour indexer sémantiquement (voir
     BRIEF-CORRECTIONS.md, section AgentCash), pas un détail cosmétique.
   - `upstream` : `url`/`method`/`sample_body` - jamais exposé publiquement.
   - `born_at` : la date du jour.

3. **Tester en local** avant tout déploiement : démarrer le serveur local
   (`PYTHONPATH=. python -m uvicorn app.main:app --port 8091`), vérifier que
   la route apparaît dans `/openapi.json`, que `/sample` répond un vrai
   résultat, que le 402 se déclenche. Un registre vide ne doit jamais casser
   les routes existantes - si ça arrive, c'est un bug dans
   `app/generated/dynamic_routes.py`, pas dans l'entrée de registre.

4. **Déployer** : même geste que d'habitude (tar en excluant `.venv`/`data`/
   `__pycache__`/`.git`, scp, extraction sous le user `x402`,
   `docker compose build && up -d`).

5. **Ne jamais publier soi-même** - c'est le travail de Crieur, qui fait
   passer Contrôleur en premier.

## Diversité, pas volume

Vingt routes sur vingt intentions valent mieux que cent sur trois. Si
Prospecteur a trouvé une source amont ouvrant plusieurs routes d'un coup,
les écrire toutes plutôt que d'en publier une seule "pour voir".
