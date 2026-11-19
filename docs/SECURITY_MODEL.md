# Security model

The point of this fork: **an AI-generated command must never be able to change the FPL team
without a human approving it.**

## Threat model

| Threat | Mitigation |
|---|---|
| Model (or a prompt injection in a web page / player news) calls a write tool directly | No write tool executes anything on its own. `propose_*` only stores a proposal. Upstream's `make_transfers` was **removed**; a test asserts it is not registered. |
| Model approves its own proposal | There is **no approve tool**. Approval is a form on the local web page that needs the operator PIN, which is never an MCP parameter. A test asserts no approve-style tool exists. |
| Model lies in the summary the human sees | The approval page renders from the **stored payload and live bootstrap data**, not from model-written text. |
| Proposal is replayed / executed twice | Single-use (`executing` claim is atomic), 15-min proposal TTL, 10-min execute window after approval. Writes are never retried by the HTTP client. |
| Squad changes between approval and execution | **Drift guard**: squad, bank and gameweek are re-fetched and fingerprinted at execution; any change blocks it. |
| PIN brute force | Constant-time compare, global lockout after 5 failures (15 min), nonce per proposal. |
| Fuzzy name matching buys the wrong player | Writes use a **strict resolver**: exact name or id only. Typos/ambiguity are refused with suggestions - never guessed. |
| Rule-breaking or reckless moves | `rules.py` validates squad shape, 3-per-club, budget, positions, availability, formation, chips; configurable caps on transfers per proposal and points hit. |
| Network exposure | Listeners bind to loopback and the app **refuses to start** on a non-loopback address unless explicitly told it is in a container. HTTP MCP **requires** a 32+ char bearer token (constant-time compare) and DNS-rebinding protection. The web app enforces a Host allow-list and strict security headers. |
| Credential theft | FPL email/password are typed only into the local login page, used once by a headless browser, and never stored or logged. Only the resulting bearer token is kept, in memory. Error text shown to users is sanitised. |
| Secrets in a public repo | `.gitignore` blocks env/keys/audit logs; CI runs gitleaks over full history; no VPS details or deploy secrets exist in the repo. |
| Silent live writes while testing | `FPL_ALLOW_WRITES` defaults to **false** (dry run: approved proposals are recorded, nothing is sent). Enabling it requires an approval PIN to be configured. |
| After-the-fact accountability | Append-only audit log of every proposal / approval / refusal / execution (never contains the PIN or tokens). |

## Layers a live change must pass

1. `rules.py` validation (cannot even create an invalid proposal)
2. Human approval with PIN on the local page
3. `FPL_ALLOW_WRITES=true`
4. Drift guard at execution
5. Single-use + expiry windows

## Emergency exception (policy only, NOT yet wired in)

`safety.SafetyPolicy` encodes the owner-specified exception: a **lineup change only**, inside the
**last 15 minutes** before a **verified** deadline, replacing a **verified suspended / ruled-out**
player with a **verified available** one, legal formation, **0 points hit, no chip**. Everything
else is denied. It fails closed on missing / unverified / mistyped data and is covered by tests.

`safety.ApprovalGate` provides HMAC-signed, expiring, single-use approvals bound to one exact
action (survives restarts; race-safe across threads and processes).

**Neither is connected to execution yet.** Today every change - including emergencies - needs the
PIN approval. Wiring an unattended emergency path requires a trusted source for "verified" deadline
and availability data and a decision on who triggers it; do that deliberately, not by default.

## Known limits (be honest about these)

* The FPL **write payload shapes are unverified against the live API.** They follow the upstream
  author's transfer model and community-documented endpoints. First live use must be a single,
  low-stakes change with the FPL website open to compare.
* Authentication works by scripting the Premier League login page. It can break when that page
  changes, and unattended automation may breach FPL's terms of use. Tokens expire; re-login is manual.
* Single-user design (one active session per process).
* Not yet implemented from the review table: FPL API timeout *state-verification before retry*
  (writes are simply never retried; check the team manually after a timeout), and an automated
  "no network in unit tests" guard (the suite makes no network calls by construction).
