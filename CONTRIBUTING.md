# Contributing

Thanks for considering a contribution. This is a small, single-purpose daemon
by design — the bar for merging is "does this make the entry-signal pipeline
more correct or more reliable," not "is this a cool feature."

## Before you start

- **Read [`CLAUDE.md`](CLAUDE.md) first.** It documents the architecture, the
  conventions that matter (batch-not-per-pool signalling, fail-open gates,
  Redis TTL semantics), and *why* things are built the way they are. A PR
  that reverts one of those conventions without discussion will likely be
  rejected — open an issue first if you think one should change.
- **Screening-gate changes need a reason.** If you're proposing a new gate or
  a threshold change, explain the failure mode it prevents or the false
  rejection it fixes in the PR description. "Seemed better" isn't enough —
  this trades real funds.
- **Scope changes narrowly.** One logical change per PR. Don't bundle a gate
  tweak with a refactor.

## Development setup

```bash
git clone https://github.com/pgen0x/meteora-dlmm-trading-bot.git
cd meteora-dlmm-trading-bot
go build -o mdtb .
go vet ./...
```

There is no CI and no Go test suite yet (see [Project Status](README.md#project-status)
in the README). Until that changes, `go build` + `go vet` passing is the
minimum bar — but manually exercise the change against a real (or `DRY_RUN`)
Hermes profile before opening a PR if it touches screening logic, the
webhook payload, or the `solana-dlmm` skill scripts.

## Commit messages

This repo follows [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

<body — the "why", not the "what">
```

Types used in this repo: `feat`, `fix`, `docs`, `chore`, `refactor`. Scope is
optional but helpful (`feat(screener): ...`, `fix(webhook): ...`,
`docs(readme): ...`). Look at `git log` for examples of the house style.

## Versioning

This project follows [Semantic Versioning](https://semver.org/)
(`MAJOR.MINOR.PATCH`):

- **MAJOR** — breaking change to the webhook payload (`docs/SIGNAL_SCHEMA.md`),
  the `.env` config surface, or the CLI.
- **MINOR** — new screening gate, new mode, new config option, backward-compatible.
- **PATCH** — bug fix, threshold tuning, docs, no behavior contract change.

The current version lives in `main.go` (`var Version`) and is reported via
`./mdtb -version`.

Releases are fully automated by
[release-please](https://github.com/googleapis/release-please) — do **not**
bump `Version`, edit `CHANGELOG.md`, or push tags by hand. Instead:

1. Write commit messages in
   [Conventional Commits](https://www.conventionalcommits.org/) form:
   `fix:` → PATCH, `feat:` → MINOR, a `!` suffix or `BREAKING CHANGE:` footer
   → MAJOR. Other prefixes (`docs:`, `chore:`, `refactor:`, ...) don't
   trigger a release.
2. On every push to `master`, the `Release Please` workflow
   (`.github/workflows/release-please.yml`) opens or updates a release PR
   that bumps `Version` in `main.go`, updates `CHANGELOG.md`, and computes
   the next semver from the commits since the last release.
3. Merging that PR creates the `vX.Y.Z` tag and GitHub Release, then
   [GoReleaser](https://goreleaser.com/) (`.goreleaser.yaml`) attaches
   cross-compiled `mdtb` binaries (Linux/macOS, amd64/arm64) with checksums.
   The version string is injected at link time.

Day-to-day PRs just need well-formed commit messages — the release PR
accumulates everything until a maintainer merges it to cut the release.

## Reporting bugs / requesting features

Open a GitHub issue. Include:
- What you expected vs. what happened.
- Relevant daemon log lines (`scanner[mode]: cycle done — ...`).
- Your `.env` config **with secrets redacted** (webhook secret, RPC keys).

## Reporting a security vulnerability

**Do not open a public issue.** Use GitHub's private security advisory
feature for this repo. See [Security](README.md#security) in the README for
what's already covered (wallet keys, RPC keys, webhook HMAC).
