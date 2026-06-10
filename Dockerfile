FROM node:24-slim

ARG CODEX_VERSION=0.139.0

RUN npm i -g @openai/codex@${CODEX_VERSION} mcp-remote

RUN apt-get update && \
    apt-get install -y --no-install-recommends bubblewrap curl jq git ripgrep ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN npx -y @playwright/mcp install-browser chromium
RUN npx playwright install-deps

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