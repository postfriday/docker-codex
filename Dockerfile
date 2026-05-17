FROM node:24-alpine3.23

RUN npm i -g @openai/codex mcp-remote
RUN apk add --no-cache bubblewrap ripgrep git

ARG VERSION
ARG REVISION
ARG SOURCE
ARG CREATED
LABEL org.opencontainers.image.version="${VERSION}}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.created="${CREATED}"