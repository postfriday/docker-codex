FROM node:24-slim

RUN npm i -g @openai/codex@0.133.0 mcp-remote
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

      ENTRYPOINT [ "/usr/local/bin/codex" ]