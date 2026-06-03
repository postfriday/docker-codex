FROM node:24-slim

ARG CODEX_VERSION=0.135.0

RUN npm i -g @openai/codex@${CODEX_VERSION} mcp-remote
RUN apt update && \
    apt install -y bubblewrap ripgrep git \
    && rm -rf /var/lib/apt/lists/*

ARG VERSION
ARG REVISION
ARG SOURCE
ARG CREATED
LABEL org.opencontainers.image.version="${VERSION}}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.created="${CREATED}"

WORKDIR /workspace

ENTRYPOINT [ "/usr/local/bin/codex" ]
