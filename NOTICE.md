# Notice

MarketsFpl is a derivative work of **fpl-mcp-server** by Lewis King
(<https://github.com/lewis-king/fpl-mcp-server>), MIT licensed (copyright notice preserved in LICENSE), taken
at commit `d83ace779463bcdcbbf93349cc0bc2ac1aeb5b00`.

Carried over largely unchanged: read-only tools, resources, prompts, models and the RotoWire
scraper (`mcp_tools.py`, `mcp_resources.py`, `mcp_prompts.py`, `models.py`, `rotowire_scraper.py`).

Changed or added in this fork: removal of the un-gated `make_transfers` tool; approval pipeline
(`approvals.py`, `rules.py`, `mcp_write_tools.py`, `safety.py`); hardened web app, transport and
configuration (`web.py`, `security.py`, `config.py`, `main.py`); client error handling and
retry-for-reads-only; login-request expiry; fixes for a missing `get_bootstrap_static`, an undefined
`logger`, and Python 3.11 f-string compatibility; tests, CI and docs.

The original licence text is preserved in [LICENSE](LICENSE).
