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
