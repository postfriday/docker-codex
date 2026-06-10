FROM node:24-slim

ARG CODEX_VERSION=0.137.0

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV NODE_PATH=/opt/codex-tools/node_modules

RUN npm i -g @openai/codex@${CODEX_VERSION} mcp-remote

RUN apt update && \
    apt install -y --no-install-recommends bubblewrap curl jq git ripgrep ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/codex-tools

RUN npm init -y && \
    npm install playwright && \
    npx playwright install --with-deps chromium && \
    chmod -R a+rX /opt/codex-tools /ms-playwright

ARG VERSION
ARG REVISION
ARG SOURCE
ARG CREATED
LABEL org.opencontainers.image.version="${VERSION}" \
    org.opencontainers.image.revision="${REVISION}" \
    org.opencontainers.image.source="${SOURCE}" \
    org.opencontainers.image.created="${CREATED}"

WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/codex"]
