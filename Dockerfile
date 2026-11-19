FROM python:3.12-slim

# uv for locked, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --python 3.12 \
 && /app/.venv/bin/playwright install --with-deps chromium \
 && chmod -R a+rX /opt/pw-browsers \
 && mkdir -p /data/audit && chown -R app:app /data /app

USER app

# Inside the container we must listen on all interfaces so the host can publish the ports; the
# CONTAINER is then the network boundary. ALWAYS publish them on the host's loopback only:
#   docker run -p 127.0.0.1:8020:8020 -p 127.0.0.1:8021:8021 ...
# Chromium's sandbox needs privileges a default container lacks; the container is the sandbox.
ENV PATH="/app/.venv/bin:$PATH" \
    FPL_MCP_TRANSPORT=streamable-http \
    FPL_WEB_HOST=0.0.0.0 FPL_MCP_HOST=0.0.0.0 FPL_ALLOW_NONLOOPBACK_BIND=true \
    FPL_BROWSER_NO_SANDBOX=true \
    FPL_AUDIT_DIR=/data/audit

# No secrets are baked in. FPL_MCP_BEARER_TOKEN and FPL_APPROVAL_PIN are required at runtime and
# are supplied only through the environment / a secret store - never the image or the repo.
VOLUME ["/data"]
EXPOSE 8020 8021

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8020/healthz', timeout=3)" || exit 1

CMD ["python", "-m", "fpl_server.main"]
