# MarketsFpl

An **approval-gated Fantasy Premier League MCP server**: let an AI assistant research, analyse and
*propose* changes to your FPL team - while only a human can approve them (one narrow, opt-in,
off-by-default emergency exception aside).

Hardened fork of [lewis-king/fpl-mcp-server](https://github.com/lewis-king/fpl-mcp-server) (MIT).
See [NOTICE.md](NOTICE.md).

> **Status: early / pre-live.** Read-only analysis and the approval pipeline are tested. The FPL
> *write* calls have **never been run against the live API** - see "Known limits" in
> [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md). Writes are **off by default** (dry run).

## What it does

* ~40 read tools from upstream: squad, bank, players, fixtures, injuries, leagues, rivals.
* **Propose tools** (change nothing): `propose_transfers`, `propose_captain`, `propose_lineup`,
  `propose_chip`.
* **`execute_approved_proposal`**: runs a proposal *only after you approved it* on a local page with
  a PIN. There is no tool that approves.
* **`emergency_lineup_swap`** (off unless `FPL_ALLOW_EMERGENCY=true`): the one exception - in the
  last 15 minutes before a verified deadline it may swap a confirmed suspended / ruled-out starter
  for an available bench player. No transfers, chips, points or captaincy; everything is verified
  server-side. See [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).
* Guardrails: rule validation (budget, 3-per-club, formation, chip windows), transfer / points-hit
  caps, strict player resolution (no fuzzy guessing on writes), drift guard, one write lock,
  single-use, expiry, unknown-outcome handling, audit log.

```
Assistant ──propose_*──▶ MarketsFpl ──▶ pending proposal
                                          │
You ──open approval page, enter PIN──────▶ approved
Assistant ──execute_approved_proposal───▶ drift check ─▶ (dry run | POST to FPL) ─▶ audit log
```

## Quick start (local, nothing exposed)

```bash
uv sync
cp .env.example .env          # local only; git-ignored; loaded automatically from the current directory
uv run python -m fpl_server.main
```

Set `FPL_APPROVAL_PIN` (>= 8 chars) to be able to approve proposals. Keep `FPL_ALLOW_WRITES=false`
until you have seen a dry run. Full options in [.env.example](.env.example), connector config and
VPS notes in [docs/DEPLOY.md](docs/DEPLOY.md).

## Development

```bash
uv run python -m pytest -q        # no network, no browser, no credentials needed
uv run python -m ruff check .
```

CI (`.github/workflows/ci.yml`) runs lint + tests on Python 3.11 and 3.12, a dependency audit, a
gitleaks scan of the full history, and a Docker build. It uses **no secrets**.

## Public-repo rules

This repo is public on purpose (free CI minutes). Therefore: no VPS details, tokens, `.env`, FPL
credentials or personal manager ids are ever committed. Deployment automation exists only as an
inert template until the repo goes private.

## Disclaimer

Unofficial; not affiliated with the Premier League. Authenticating by scripting the FPL login may
conflict with their terms of use and can break without notice. Use at your own risk.
