FROM node:24-slim

ARG CODEX_VERSION=0.145.0
ARG OPENCODE_VERSION=1.17.9
ARG CLAUDE_CODE_VERSION=2.1.187

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        bubblewrap \
        ca-certificates \
        curl \
        git \
        jq \
        ripgrep \
        shellcheck \
    && rm -rf /var/lib/apt/lists/*

RUN npx -y @playwright/mcp install-browser chromium && \
    npx playwright install-deps && \
    git clone https://github.com/atomkraft/yandex-metrika-mcp.git /opt/yandex-metrika-mcp && \
    cd /opt/yandex-metrika-mcp && \
    npm install

RUN npm i -g @openai/codex@${CODEX_VERSION} mcp-remote

RUN npm i -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

RUN npm i -g opencode-ai@${OPENCODE_VERSION}

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
