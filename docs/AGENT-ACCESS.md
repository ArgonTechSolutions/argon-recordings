# Account-scoped agent access

The local stdio MCP adapter uses a dedicated project grant. It does not reuse browser cookies, worker tokens or administrator MFA credentials. Account mode is required; there is no anonymous agent fallback.

In the Account dialog, **Agent access** lets the signed-in user choose a project, expiry (1–30 days) and optional permission to request screenshots/clips. The credential is shown once in a masked field, held only in the mounted view, and cleared on close. The database stores its SHA-256 digest. Lists, logs and audit entries do not expose the credential. Only the grant's user can revoke it; administrator status does not grant implicit content access.

Every operation rechecks the grant, account status and current project membership. Revocation, expiry, disabled users and removed memberships deny subsequent requests, including images. Derived-media requests additionally require both the grant permission and a current editor/owner role. A viewer can receive a read grant but cannot grant extraction rights.

The API namespace `/api/agent/*` accepts only these grants; browser and worker routes retain their existing authentication. The adapter exposes twelve scoped evidence tools. Native images come from registered, authorized image IDs; no arbitrary path, URL, shell command, administration or transcript-edit tool is exposed. Extraction returns a durable job and uses the existing bounded worker queue. The agent can inspect/cancel authorized visual tasks.

Transcript pages preserve source/transcript identity and reject stale or mismatched cursors. Pages have character/word/item limits; a single oversized segment with word details returns a bounded diagnostic and supports a text-only retry. Image replies are limited to 4 MiB; the adapter bounds JSON to 1 MiB. Content is untrusted evidence, not instructions for the receiving agent.

`scripts/agent-integration/README.md` documents the optional pinned SDK, private credential-file configuration and local checks. No real credential was provisioned or installed during automated development. Remote OAuth/Streamable HTTP and cloud reachability remain separate future work.

## Local qualification

- The full backend suite, including prepared E5-large and actual FFmpeg tests, passed 222 tests; the OpenAPI contract check passed.
- Production frontend build and lint passed. Chromium exercised ephemeral token creation/hiding, storage exclusion, revocation and the 320/390 px dialog, alongside the existing account/MFA lifecycle.
- Four published-SDK tests passed: protocol fixtures plus a real temporary API/account/database test. The latter requests a screenshot through MCP, runs the actual visual worker/FFmpeg, retrieves and decodes the registered JPEG through MCP, and verifies denied/revoked access.
- Independent GPT-6.1 review of the new backend authorization boundary found a first-segment context-budget bypass. It was fixed, regression-tested and re-reviewed without remaining findings in that fix.

These establish local source/runtime behavior. They do not establish owner pairing, private-recording acceptance, installed Philemon behavior, public connectivity or semantic summary quality. The summary skill, report workflow and final release qualification remain separate milestones.

The optional summary skill/private package now has a local implementation and bounded forward-test; see [SUMMARY-WORKFLOW.md](SUMMARY-WORKFLOW.md). Project and search tools expose their API cursors, and search returns the paginated object rather than an incorrectly declared list. The combined adapter/backend/summary package regression passed 24 tests. Host installation, live pairing, report export and integrated semantic qualification remain separate gates.

P12 adds explicit browser report import/review and portable exports while retaining the same read/derive-only MCP authority. The shared summary contract and new draft CLI are included in private package version 0.2.0. The integrated SDK/backend/package suite now passes 25 tests, including actual MCP evidence → explicit browser report save/review → checksummed ZIP. See [REPORTS.md](REPORTS.md). Live owner pairing, host installation and integrated semantic/release qualification remain separate gates.
