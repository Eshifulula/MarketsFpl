# Security policy

Design and threat model: [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab -> "Report a vulnerability")
rather than a public issue. Do not post credentials, tokens or squad data anywhere public.

## Ground rules for contributors

* Never commit secrets, `.env` files, keys, audit logs, VPS addresses or manager ids.
* No code path may change an FPL team without a recorded human approval, except the documented,
  opt-in emergency lineup swap (`mcp_emergency_tools.py`). Any widening of that exception needs a
  design discussion first.
* New write behaviour needs tests proving it cannot bypass `approvals.py` (or, for the emergency
  path, `safety.SafetyPolicy`), and must go through the shared `runtime.execution_lock()`.
* Only `mcp_write_tools.py` and `mcp_emergency_tools.py` may call the FPL write endpoints; a test
  enforces this.
