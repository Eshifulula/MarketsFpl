# Security model

The point of this fork: **an AI-generated command must never be able to change the FPL team
without a human approving it** - with one narrow, opt-in, off-by-default exception (below).

## Threat model

| Threat | Mitigation |
|---|---|
| Model (or a prompt injection in a web page / player news) calls a write tool directly | `propose_*` only stores a proposal. Upstream's `make_transfers` was **removed**. A test pins the exact set of write-capable tools and asserts only two modules may call the FPL write endpoints. |
| Model approves its own proposal | There is **no approve tool**. Approval is a form on the local web page that needs the operator PIN, which is never an MCP parameter. |
| Model lies in the summary the human sees | The approval page renders from the **stored payload and live bootstrap data**, not model-written text. |
| Proposal replayed / executed twice / two approvals racing | Single-use atomic claim; proposal TTL 15 min, execute window 10 min; a **new proposal cancels any older pending or approved-but-unexecuted one**; **one lock** serialises every FPL write (approved proposals *and* the emergency tool). Writes are never retried by the HTTP client. |
| Approval outlives the gameweek | At execution the gameweek table is **re-fetched**; the proposal is refused if the gameweek rolled over or the deadline has passed. |
| Squad changes between approval and execution | **Drift guard**: squad order, captaincy, prices, bank and gameweek are re-read under the lock and fingerprinted; any change blocks execution. |
| PIN brute force | Constant-time compare, global lockout after 5 failures (15 min), per-proposal nonce. |
| Fuzzy name matching buys the wrong player | Writes use a **strict resolver**: exact name or id only; typos/ambiguity are refused with suggestions. |
| Rule-breaking or reckless moves | `rules.py` validates squad shape, 3-per-club, budget, positions, availability, formation, chip windows; caps on transfers per proposal and points hit. |
| Network exposure | Loopback binds; the app **refuses to start** on a non-loopback address unless told it is in a container. HTTP MCP **requires** a 32+ char bearer token and DNS-rebinding protection. The web app enforces a Host allow-list and strict security headers. |
| Credential theft | FPL email/password are typed only into the local login page, used once by a headless browser, never stored or logged. Only the resulting token is kept (memory only). User-facing errors are sanitised. |
| Secrets in a public repo | `.gitignore` blocks env/keys/audit logs; CI runs gitleaks over full history; no VPS details or deploy secrets exist in the repo. |
| Silent live writes while testing | `FPL_ALLOW_WRITES` defaults to **false** (dry run). Enabling it requires an approval PIN. |
| Ambiguous outcome (timeout / 5xx / parse error after sending) | Reported as **`outcome_unknown`** (never "failed"), the proposal is finalised so it cannot be re-sent, and the user is told to check the FPL website. A 2xx POST with a non-JSON body is treated as accepted. |
| After-the-fact accountability | Append-only audit log of every proposal / approval / refusal / execution. It never contains the PIN or tokens. |

## Layers a normal (non-emergency) live change must pass

1. `rules.py` validation - an invalid proposal cannot even be created
2. Human approval with PIN on the local page
3. `FPL_ALLOW_WRITES=true`
4. Under the shared lock: fresh gameweek + squad fingerprint must match, deadline not passed
5. Single-use + expiry windows; never retried

## The emergency exception (opt-in, off by default)

`emergency_lineup_swap(player_out, player_in)` is the **only** tool that can change the team without
a PIN. It is disabled unless `FPL_ALLOW_EMERGENCY=true`, and it is still a dry run unless
`FPL_ALLOW_WRITES=true`. Startup logs a loud warning when it is on.

**Scope** (decided by `safety.SafetyPolicy`, fail-closed): swap ONE confirmed suspended / ruled-out
**starter** for ONE available **bench** player; within **15 minutes** of a **verified** deadline (and
never closer than **60 s**, to leave margin for latency); legal formation; **0 points hit**; **no
chip**; **no captaincy change** (if the player is captain or vice-captain it refuses).

**The model cannot assert any fact.** It passes two names. The server derives, from fresh official
FPL data: the deadline; each player's availability (`s` = suspended; `i`/`u`/`n` count as ruled out
only with an explicit 0% chance of playing, otherwise they are merely "doubtful"); the formation;
and chip state. Facts are "verified" only if the FPL response is recent (CDN `Age` <= 180 s) **and**
our clock agrees with FPL's `Date` header (|skew| <= 30 s, in both directions). Missing, empty or
malformed data - including chip data - is treated as unverified and the policy denies.

**Additional guards on this path:** the shared write lock; the squad is **re-read right before the
POST** and must be unchanged; the audit log must be writable or nothing is sent; a **per-gameweek
cap** (default 2) stored on disk, consumed just before sending (never by dry runs); the POST is
never retried; ambiguous results are reported as unknown.

`safety.ApprovalGate` (HMAC-signed, expiring, single-use approvals bound to one exact action) exists
and is tested but is **not used by the live path** - the PIN page is.

## Known limits (be honest about these)

* The FPL **write payload shapes are unverified against the live API.** They follow the upstream
  author's transfer model and community-documented endpoints. First live use must be one low-stakes
  change with the FPL website open to compare. The emergency path in particular has never run live.
* "Verified" means "from FPL's own API, fresh, and consistent with our clock". FPL's status flags
  can themselves lag real-world news; the emergency path cannot be better than that data.
* Authentication works by scripting the Premier League login page. It can break when that page
  changes, and unattended automation may breach FPL's terms of use. Tokens expire; re-login is manual.
* Single-user, single-process design: the write lock is in-process. Do not run two instances against
  one account.
* Not covered: automatic state verification before a retry after a timeout. Writes are simply never
  retried; after `outcome_unknown`, check the team manually.
