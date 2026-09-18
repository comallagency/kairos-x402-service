FROM python:3.12-slim

WORKDIR /app

# Node.js/npm - only for `npx -y @agentcash/discovery` and `npx -y agentcash`,
# which scripts/controleur.py shells out to (the real validators, not a
# reimplementation - see BRIEF-CORRECTIONS.md on why that matters). Confined
# to this image only, never installed on the shared VPS host.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir \
    "x402[extensions]==2.22.0" \
    cdp-sdk \
    fastapi \
    "uvicorn[standard]" \
    httpx \
    python-dotenv \
    pydantic-settings \
    email-validator \
    fastmcp==4.0.3 \
    pyyaml \
    trafilatura \
    pypdf \
    jsonschema \
    py3langid \
    tiktoken

COPY app ./app
COPY scripts ./scripts
COPY chain_payments.py send_daily_report.py ./

# Bakes tiktoken's cl100k_base encoding into the image at build time - without
# this, tiktoken.get_encoding() downloads it from OpenAI's CDN on first use in
# production, a runtime network dependency this project has no other reason
# to take on (see app/upstream/tokencount.py).
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

# UID matches the host's x402 system user (1003) so the bind-mounted
# ./data and ./logs volumes are actually writable without permission hacks.
RUN useradd -m -u 1003 appuser && mkdir -p /app/data /app/logs && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
# --proxy-headers is on by default, but uvicorn only trusts X-Forwarded-* from
# forwarded_allow_ips (default 127.0.0.1) - inside Docker the peer seen by
# uvicorn is the bridge gateway, not 127.0.0.1, so nginx's headers were being
# ignored. Port 8000 is only ever reached via the host's loopback-bound
# 127.0.0.1:18402 -> nginx, so trusting all peers here is safe.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*", "--timeout-graceful-shutdown", "15"]
