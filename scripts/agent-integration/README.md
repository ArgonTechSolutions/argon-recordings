# Argon Recordings MCP adapter

This local stdio server exposes only the project-scoped `/api/agent/*` contract. It reads the fixed API origin and bearer grant from process configuration; tool schemas accept no credential, API URL, filesystem path, shell command, or arbitrary endpoint. The backend grant remains authoritative and can be revoked independently.

Install the pinned Python SDK in a private Python 3.11+ environment:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r scripts/agent-integration/requirements.txt
```

Configure the host to start `scripts/agent-integration/server.py` with that interpreter and these environment variables:

- `ARGON_RECORDINGS_API_ORIGIN`: fixed origin such as `https://recordings.example`; plain HTTP is accepted only for loopback tests.
- `ARGON_AGENT_TOKEN_FILE`: private regular file containing the issued `rec_agent_…` grant, with mode `0600` and no symlink.

For disposable loopback interoperability tests only, `ARGON_AGENT_TOKEN` may be used with `ARGON_AGENT_ALLOW_ENV_TOKEN=1`. It is rejected for non-loopback origins. Do not place grants in tool arguments, committed host configuration, logs, or model-visible resources.

The adapter disables environment proxies and redirects, verifies TLS, limits JSON responses to 1 MiB and JPEGs to 4 MiB, and returns screenshots as native MCP image content. Requesting a screenshot or clip creates an asynchronous derivation job; it does not alter the source recording.

Run the real SDK stdio interoperability suite in Python 3.11+:

```sh
python -m pip install -r scripts/agent-integration/requirements.txt pytest
python -m pytest -q scripts/agent-integration/test_interop.py
```

The suite launches the adapter as a stdio subprocess and talks to it using the published MCP Python SDK client. Its temporary loopback HTTP server supplies fixed protocol fixtures; it does not prove deployed API behavior or live grant interoperability.

## Real backend integration check

`test_live_backend.py` runs the published SDK through the stdio adapter against a real, temporary FastAPI/SQLite account instance. It creates a disposable account and project grant, follows transcript pagination, receives and decodes an actual FFmpeg JPEG, checks denied access, and verifies grant revocation. The API listener and SDK subprocess terminate when the test finishes. No owner credentials or installation data are used.

```sh
PYTHONPATH=backend python -m pytest -q scripts/agent-integration/test_live_backend.py
```

This check also requires the backend test dependencies and FFmpeg. The repository's `scripts/Dockerfile.validation` provides them. It proves local protocol/backend integration; owner pairing, remote connectivity and semantic summary quality remain separate acceptance gates.

## Summary skill and private local package

The repository-owned `plugin/skills/recording-summary/SKILL.md` orchestrates scope/language selection, transcript pagination, manual speaker/mark review, actual image inspection, optional bounded media requests, citations and a final revision check. Its portable JSON report contract is under that skill's `scripts/summary_contract.py`; deterministic checks do not prove semantic truth. The skill returns a summary to the user by default.

Build a self-contained private package with an explicit output path outside the source directory:

```sh
python scripts/agent-integration/build_plugin.py /tmp/argon-recordings.zip
```

The ZIP contains the portable `plugin.json`, `mcp.json`, skill, adapter and pinned requirements, using an explicit file allowlist. It contains no recordings, credentials, virtual environments or dependency downloads. The source `plugin/` directory is a build input; the builder adds the existing adapter rather than keeping a second copy in Git.

For an owner-authorized local install, unpack the ZIP to a private chosen directory, install `requirements.txt` into a Python 3.11+ virtual environment and let the client start `server.py` with that interpreter. The portable manifest uses `python3` from the host's configured `PATH`; ensure it selects the prepared environment, or configure the client's interpreter explicitly. Supply only `ARGON_RECORDINGS_API_ORIGIN` and `ARGON_AGENT_TOKEN_FILE` through private host configuration as described above. `${PLUGIN_ROOT}` is resolved by the plugin host. No concrete API origin, token destination, global plugin registration, account upload or live connection is created by this build.

For a client using an explicit MCP server configuration, adapt this credential-free example to the chosen private installation (paths and origin are placeholders, not provisioned locations):

```json
{
  "mcpServers": {
    "argon-recordings": {
      "command": "/chosen/venv/bin/python",
      "args": ["/chosen/argon-recordings-plugin/server.py"],
      "env": {
        "ARGON_RECORDINGS_API_ORIGIN": "https://recordings.example",
        "ARGON_AGENT_TOKEN_FILE": "/chosen/private/agent-grant"
      }
    }
  }
}
```

The report checker also requires the prepared interpreter: use `/chosen/venv/bin/python …/summary_contract.py`, rather than assuming the system Python has Pydantic. The pinned MCP SDK supplies that dependency.

The package now includes the canonical backend `report_contract.py` next to the skill's CLI wrapper, so source, packaged and API validation share one implementation. For a requested importable draft, use its `draft` command with the report, inspected-evidence ledger, final reference and truthful generator identity; see the skill's output contract. The app's **Reports** controls explicitly save/review the file and export the selected evidence ZIP. Agent grants do not gain report-write permissions. See [REPORTS.md](../../docs/REPORTS.md) for bounds, contents and freshness semantics.

Run package startup, report-schema and adversarial structural checks:

```sh
PYTHONPATH=backend python -m pytest -q scripts/agent-integration/test_interop.py scripts/agent-integration/test_live_backend.py scripts/agent-integration/test_summary.py
```

`test_live_backend.py` can additionally capture disposable SDK evidence when `SUMMARY_EVIDENCE_DIR` is set to a private local output directory. That capture includes source text and a screenshot, never its grant. Use it for a bounded agent forward-test; do not treat replay of captured responses as a live client connection, a fresh revision check or a representative recording-quality test.
