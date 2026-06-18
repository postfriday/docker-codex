FROM node:24-slim

ARG CODEX_VERSION=0.141.0

RUN npm i -g @openai/codex@${CODEX_VERSION} mcp-remote

RUN apt-get update && \
    apt-get install -y --no-install-recommends bubblewrap curl jq git ripgrep ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN npx -y @playwright/mcp install-browser chromium && \
    npx playwright install-deps && \
    git clone https://github.com/atomkraft/yandex-metrika-mcp.git /opt/yandex-metrika-mcp && \
    cd /opt/yandex-metrika-mcp && \
    npm install

RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"

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
