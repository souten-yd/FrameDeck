# Agent instructions

## Releasing FrameDeck

When asked to release, read [docs/RELEASE.md](docs/RELEASE.md) first.
Use the existing non-browser release path. If release-create or workflow-dispatch
tools are unavailable, use the documented one-shot workflow on an isolated
`ci/release-<version>` branch. A missing direct tool does not mean that browser
interaction is required. Do not merge the one-shot workflow into main or enable
CI on ordinary pushes/PRs/tags. Confirm the successful run, published release,
tag target, and attached QPKG before reporting completion.
