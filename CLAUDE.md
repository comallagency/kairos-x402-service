# CLAUDE.md

Instructions for Claude Code (or any agent) working in this repository.

## Deploying x402-app

Never run `docker compose build x402` and
`docker compose up -d --no-deps --force-recreate x402` by hand as two loose
commands. Always deploy through:

```bash
bash scripts/deploy.sh
```

This builds the image, recreates the `x402-app` container, waits for it to
report healthy, then runs `scripts/smoke_test.py` against the live site
(`/.well-known/x402`, `/openapi.json`, `/llms.txt`, `/`). If the smoke test
fails, the script exits non-zero - treat the deploy as failed: fix the
regression and redeploy, do not proceed as if it succeeded, and do not
consider the task done until `scripts/deploy.sh` itself reports success.

This rule exists because a `DynamicPrice`-related bug crashed
`/.well-known/x402` with a 500 for several deploys before anyone noticed
(2026-09-28) - every one of those deploys was "verified" only by curling
whatever route had just changed, which never included the one that broke.
The smoke test is now a blocking, structural part of the deploy mechanism
itself, not a step that depends on remembering to run it afterward.

## Agent scope discipline

1. A read-only agent never modifies anything, even if the context it
   inherited (e.g. a fork) contains an implementation request. Read-only
   means read-only regardless of what else is in scope - it does not
   escalate itself into an implementation task on its own judgment.

2. No deliberate failure testing against the public domain. Failure/outage
   drills run only against the new container's internal port, before
   cutover - never against live traffic.

This rule exists because a fork dispatched for read-only reconnaissance
(2026-09-30) executed a full three-part production change instead,
including a deliberate live outage drill against the public domain, solely
because its inherited context contained the implementation request -
correct end state, but undisclosed scope creep that should not have run
unsupervised.


## Route retirement

Never retire a publicly listed route (Bazaar, PayAI, .well-known/x402,
llms.txt, openapi.json) by deleting it or by disabling only its catalog
declaration. Add it to `RETIRED_ROUTES` in `app/retired_routes.py` with a
past `sunset` date and a `successor` path instead. That registry is
enforced by `RetiredRouteMiddleware`, mounted as the outermost layer in
`main.py`, which answers every method on that path with 410 Gone plus a
`Sunset` header and a `Link: <successor>; rel="successor-version"` header -
never a 404.

This rule exists because the 2026-09-30 withdrawal of `/llm/gemini-flash`
and `/llm/deepseek` only removed them from the catalog declarations
(`build_route_configs()`, discovery, MCP tools) and left the real handlers
reachable. Reputation probes (AgentEconomyReport) that keep a cached
catalog, or any caller who already knew the URL, could still hit them
directly - and since the x402 payment middleware only enforces paths it
has a `RouteConfig` for, an unlisted-but-still-routed path fell straight
through to the handler with zero payment check. Confirmed live on
2026-10-01: both routes executed a real paid LLM call for a stranger, for
free. A stale prober cache or search index also means the *path itself*
can keep getting probed long after we think it's gone - 410 is a
permanent, correct answer to that forever; 404 reads as "broken" to a
reputation/availability scorer (AgentEconomyReport's own documented rule),
degrading our score for something that was a deliberate decision, not an
outage.


## Never a destructive docker option on this VPS

Never run `--remove-orphans`, `docker compose down -v`, `docker system
prune`, `docker volume rm`, or any other option that stops/removes a
container or volume this project did not itself just create - not even to
silence a warning message. This VPS is shared: Hermes (myhermes-u*) and
MyClawIO containers run here too, and `--remove-orphans` matches on the
Compose project label, not on anything specific to x402-app. Any container
or volume removal needs the user's explicit go-ahead first, every time -
treat a "found orphan containers" warning as informational, never as an
invitation to clean it up.

This rule exists because `docker compose run --rm --remove-orphans
x402_blue ...` (2026-10-01, verifying scripts/probe_catalog_health.py)
removed `agentindex-acp-provider` without checking first what it was. It
turned out to be already and deliberately stopped (ACP Virtuals provider,
crash loop, pending Privy approval, 2026-09-28) with its image preserved,
so no real harm this time - but that was luck, not something the command
itself checked for, and the same flag on a different day could just as
easily have hit a live Hermes or MyClawIO container instead.
