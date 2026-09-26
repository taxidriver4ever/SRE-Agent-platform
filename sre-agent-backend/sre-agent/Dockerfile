FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm

ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.source="https://github.com/taxidriver4ever/SRE-Agent-platform" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.title="sre-agent"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/usr/local/bin:${PATH}"

# Git is required by the read-only code-state and validation features. Node/npm
# are copied from the official Node image for the pinned Kubernetes MCP server.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /app
COPY sre-agent-backend/sre-agent/requirements.txt /tmp/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /tmp/requirements.txt

COPY sre-agent-backend/sre-agent/app ./app
COPY sre-agent-backend/sre-agent/config ./config
COPY sre-agent-backend/sre-agent/scripts ./scripts
# The catalog is configuration, not a bundled runtime dependency. Git source is
# resolved from allow-listed repository annotations into the writable cache.
COPY sre-broken-system/sre-lab-infra/service-catalog.yaml /opt/sre-broken-system/sre-lab-infra/service-catalog.yaml

RUN groupadd --system --gid 10001 sre \
    && useradd --system --uid 10001 --gid sre --home-dir /home/sre --create-home sre \
    && mkdir -p /var/lib/sre-agent/repositories /var/lib/sre-agent/validation /var/lib/sre-agent/artifacts /home/sre/.npm \
    && chown -R sre:sre /app /opt/sre-broken-system /var/lib/sre-agent /home/sre

USER 10001:10001
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=2).read()"]

CMD ["python", "-m", "app.serve"]
