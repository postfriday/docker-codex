# Repository Guidelines

## Project Structure & Module Organization

This repository packages AI command-line tools in a Docker image based on `node:24-slim`.
- `Dockerfile` installs Codex, Claude Code, OpenCode, browser tooling, and Docker; `/workspace` is the working directory and Codex is the entrypoint.
- `Taskfile.yml` defines local builds, checks, and releases.
- `VERSION` holds the image's semantic version, independently of tool versions in Dockerfile build arguments.
- `.github/workflows/build-push.yml` publishes AMD64/ARM64 images to GHCR with provenance, SBOM generation, and vulnerability scanning.
- `README.md` documents runtime permissions and releases. There are no application source, test, or asset directories.

## Build, Test, and Development Commands

Install Docker and Task locally; linting and scanning additionally require Hadolint and Trivy.
- `task` lists available tasks; `task version` prints computed build metadata.
- `task lint` checks the Dockerfile with Hadolint.
- `task build` builds for the current platform, tagging `docker-codex:dev` and a version/SHA tag.
- `task run` builds and launches an interactive container; it currently omits the sandbox permissions documented below.
- `task scan` builds and scans for HIGH/CRITICAL vulnerabilities.
- `task clean` removes the computed local image tags.

## Coding Style & Naming Conventions

Use two-space YAML indentation. Match the Dockerfile's uppercase instructions, four-space continuation indentation, and multiline package lists. Keep tool versions in named `ARG` values such as `CODEX_VERSION`. Use lowercase Task names and uppercase build/configuration variables. Follow existing `set -eu` and quoted-variable patterns in shell blocks. Hadolint is the configured linter; no formatter is configured.

## Testing Guidelines

There is no automated test suite or coverage threshold. For image changes, run `task lint`, `task build`, and `docker run --rm docker-codex:dev --version`; exercise any changed tool explicitly. Run `task scan` for dependency changes. Include results in the PR. Publication CI runs on version tags or manual dispatch, not pull requests.

## Commit & Pull Request Guidelines

Follow recent history: `fix:`, `feat:`, `docs:`, and `chore:` with concise imperative descriptions. PRs should explain the change, affected tool versions, validation results, and relevant issues. Release separately with `task bump` and `task release`; release requires only `VERSION` to be changed and creates `vX.Y.Z` tags. Pushing a version tag triggers publication.

## Security & Configuration Tips

Keep credentials out of commits; `.env` is excluded from Docker context, not Git. For bubblewrap sandbox operation, use `--cap-add SYS_ADMIN --security-opt seccomp=unconfined` as documented in README; these grant elevated container permissions.
