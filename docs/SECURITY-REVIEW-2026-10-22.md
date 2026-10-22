# Administrator authentication review — 2026-10-22

Scope: the private account/admin implementation based on `92a144b`, plus this passkey and HTTP-boundary change. Review covered password/OIDC -> admin second factor, enrollment, origin/session binding, challenge replay, recovery, session revocation and credential-change transactions. The accepted independent review used GPT-6.1 Sol. This is static code review and disposable automated evidence, not penetration testing, real-authenticator qualification or public deployment approval.

## Findings and disposition

- Password-only administrative sessions previously granted instance access. Admin API reads and writes now require second-factor proof. First passkey enrollment additionally requires a host-issued, user/RP-bound, ten-minute code; passwords and password-reset links cannot authorize first enrollment alone. OIDC uses the same second-factor gate.
- The first implementation allowed recovery sessions to remove existing passkeys. Removal now requires recent passkey verification, including inside its database write transaction, and retains a final key per hostname.
- The first implementation trusted request-entry session state for credential mutations. Code replacement, key deletion and additional enrollment now recheck current enabled administrator, live session, expiration and fresh proof inside serialized write transactions. Tests revoke the session between middleware admission and the write and confirm no credential mutation.
- Lost-key recovery intentionally permits password/OIDC plus a one-time recovery code to enroll a replacement key. Replacement rotates all remaining recovery codes and revokes other admin sessions. This is documented recovery authority, rather than a claim that recovery sessions are read-only. All-key loss also has explicit host-terminal recovery.
- The dependency audit found the existing Starlette 0.48.0 affected by Host/path reconstruction and file-range advisories. FastAPI and Starlette are now pinned to compatible 0.159.1/1.9.0 releases. HTTP admission rejects malformed/duplicate Host authorities before using a reconstructed URL; authorization uses ASGI router paths. Regression tests cover embedded paths, query/fragment separators, userinfo, malformed ports, backslashes and duplicate Host. Primary references: [Starlette Host advisory](https://github.com/Kludex/starlette/security/advisories/GHSA-86qp-5c8j-p5mr), [path advisory](https://github.com/Kludex/starlette/security/advisories/GHSA-jp82-jpqv-5vv3), [framework release compatibility](https://fastapi.tiangolo.com/release-notes/).

Focused independent re-review found no remaining material finding in this scope. That result does not clear the blockers below.

## Evidence

- Full backend suite in a bounded disposable API container: 99 passed, one opt-in real-embedding integration skipped. The changed auth dependency graph does not require rerunning speech-accuracy benchmarks. Existing HTTPX/Authlib deprecation warnings are retained; no peer-dependency override was added.
- Signed synthetic ES256 registration and authentication responses use the actual py_webauthn verifier; tests reject wrong origins, missing user verification, invalid signatures, expired/replayed and cross-session challenges. Recovery codes are hashed, one-use, rate-limited and rotated on recovery enrollment.
- Actual Chromium WebAuthn API with a virtual user-verifying authenticator exercises initial host-authorized enrollment, recovery-code acknowledgment on a mobile viewport, subsequent passkey verification, recovery sign-in, admin/app separation, invitations, project membership, viewer permissions and live SSE revocation. No console errors. Virtual authenticators do not establish real Windows Hello, device or password-manager compatibility.
- Actual generated OpenAPI contract/types, frontend production build and lint checked. The framework upgrade consolidates `CorrectionView-Output` into `CorrectionView`; the correction UI follows the generated contract.
- `pip-audit -r backend/requirements.txt` checked 29 resolved API/auth dependencies against the current advisory service: no known vulnerabilities reported after remediation. This is not an audit of OS packages, FFmpeg, inference dependencies, images or JavaScript dependencies. Production image-wide dependency qualification remains required.

Local ignored evidence is under `.recordings/mfa-*`; synthetic passkey/account test data and generated enrollment/recovery values are not distributed in Git. No real administrator credentials were created and no live data was migrated by these checks.

## Public release remains blocked

The bundled API and untrusted media worker currently share a container, process environment and account data volume. The public profile must isolate the worker from account/session data and qualify hostile inputs/resource use. Gateway worker-route denial, exact HTTPS/proxy/cookie/header behavior, upload quotas/resume/reservations, trusted rate-limit attribution, backup/restore and real-Philemon acceptance also remain. See [the Forculus/Tailscale draft](PUBLIC-ACCESS-DRAFT.md). No public listener, DNS or server configuration was changed.
