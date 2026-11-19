# Deploying (later - not while the repo is public)

**Rule:** no VPS address, credentials, keys, tunnel tokens or `.env` files in this repository, its
issues, PRs or Actions config while it is public. CI in this repo uses no secrets at all.

## Run locally today (nothing exposed)

```bash
uv sync
cp .env.example .env        # fill in locally; .env is git-ignored
# stdio mode for Claude Desktop, dry-run writes, approvals disabled until you set a PIN
uv run python -m fpl_server.main
```

Claude Desktop config (stdio):

```json
{ "mcpServers": { "fpl": {
    "command": "uv",
    "args": ["run", "--directory", "/ABSOLUTE/PATH/TO/MarketsFpl", "python", "-m", "fpl_server.main"]
} } }
```

## Going live on the VPS (do this after the repo is private)

1. Make the repository private.
2. Build the image (`docker build -t markets-fpl .`). Nothing is pushed to a public registry.
3. Run it with ports published **on loopback only** and secrets from the VPS environment / secret
   store, never from the repo:
   ```bash
   docker run -d --name markets-fpl --restart unless-stopped \
     -p 127.0.0.1:8020:8020 -p 127.0.0.1:8021:8021 \
     -e FPL_MCP_BEARER_TOKEN -e FPL_APPROVAL_PIN \
     -v markets-fpl-data:/data markets-fpl:<exact-commit-tag>
   ```
4. Reach it through an authenticated tunnel (e.g. Cloudflare Access / Tunnel with a service token,
   or an SSH tunnel). Never open 8020/8021 to the internet. The **approval page (8020) should be
   reachable only by you**.
5. Add the tunnel hostname to `FPL_WEB_ALLOWED_HOSTS` / `FPL_MCP_ALLOWED_HOSTS`.
6. Keep `FPL_ALLOW_WRITES=false` and `FPL_ALLOW_EMERGENCY=false` for the first week. Review the audit
   log in `/data/audit`. Turn on `FPL_ALLOW_EMERGENCY` last, and only on purpose: it lets a verified
   emergency lineup swap run **without** your approval (see docs/SECURITY_MODEL.md).
7. First live change: one low-stakes transfer or a captain swap, with the FPL website open.

## Enabling automated CD (optional, after going private)

`deploy/deploy.workflow.yml.template` is an inert, manual-only template. Its header lists the
exact steps (protected `production` environment with required reviewers, restricted deploy key,
`DEPLOY_ENABLED` variable). It never runs two deployments at once and hands the restart script the
**exact commit-tagged image** (no shared mutable tag). Move it into `.github/workflows/` only then.

## Connector compatibility note

HTTP mode is protected by a static bearer token, which suits Claude / MCP clients that can send
an `Authorization` header and Cloudflare Access service tokens. ChatGPT's custom connector expects
OAuth or no auth, so connecting ChatGPT needs an OAuth front door - a separate piece of work.
