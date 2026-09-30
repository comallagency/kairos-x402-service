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
