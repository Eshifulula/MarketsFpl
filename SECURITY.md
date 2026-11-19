# Security policy

Design and threat model: [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab -> "Report a vulnerability")
rather than a public issue. Do not post credentials, tokens or squad data anywhere public.

## Ground rules for contributors

* Never commit secrets, `.env` files, keys, audit logs, VPS addresses or manager ids.
* No code path may change an FPL team without a recorded human approval.
* New write behaviour needs tests proving it cannot bypass `approvals.py`.
