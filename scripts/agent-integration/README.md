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
