# Optional authenticator apps: security review

Scope: TOTP changes after `a40674c`, reviewed independently with GPT-6.1 Sol. No material findings remained after the final scoped review. This is code review and disposable automated evidence, not penetration testing, real-phone acceptance or public deployment approval.

## Security contract

- `auth.allow_totp=false` is the installation default. Passkeys stay preferred. Opting in explicitly permits a phishable code-based administrator second factor; it does not make TOTP phishing-resistant or require a Google account.
- First enrollment requires an authenticated administrator and an operator-issued code bound to that account/hostname. A recent already-verified factor can authorize additional enrollment. An unconfirmed QR grants no admin proof. Pending encrypted setup is session/origin/account/hostname-bound and expires after ten minutes. Successful confirmation atomically consumes the operator ticket and activates the factor.
- PyOTP implements the six-digit SHA-1/30-second RFC 6238 codes. Verification is bounded to the current/adjacent time steps, rate-limited per administrator, and advances a transactional per-factor counter. A successfully accepted code cannot be reused across sessions, including after restart; confirmation also consumes its time step.
- Shared secrets are encrypted with maintained Fernet authenticated encryption and bound to account/hostname. The automatically managed `0600` key is under `/data/auth/totp.key`. Missing/mismatched/corrupt/insecurely permissioned keys fail closed, rather than silently replacing an active key. Complete data-volume backup/restore and host custody remain necessary. This does not protect against compromise of the whole host/data volume.
- Successful verification rotates the admin session. Revocation/freshness is rechecked inside credential-write transactions. Removing the app requires another passkey at that hostname and revokes admin sessions. Disabling the policy permanently deletes TOTP-verified sessions; re-enabling cannot revive their cookies. Existing encrypted enrollments are retained for later sign-in.
- Recovery codes work for TOTP-only accounts too. Recovery sessions can recover through a new passkey, but cannot remove factors or generate new recovery codes until an actual authenticator is verified. Host recovery clears both factor types and pending setup explicitly. App sessions and project/model storage are preserved.
- QR generation is local using Segno. Secret-bearing responses receive `no-store`, require session/CSRF authorization and do not use third-party QR services. Provisioning values stay in component memory; no browser storage/query cache is used. Logs/screenshots must not capture enrollment/recovery material.

## Validation

- Complete backend suite: 109 passed, one optional real-embedding test skipped. Existing HTTPX/Authlib deprecation warnings remain. Tests cover opt-out policy, installer validation/env propagation, first enrollment authority, encryption/key permissions and restore failure, one-use replay, session-bound pending setup, expiration/cancel, rate limiting, factor retention, TOTP-only recovery, disabling/re-enabling sessions, operator recovery and revoked-session races.
- Actual Chromium TOTP harness: mobile QR/manual-key setup and confirmation, recovery acknowledgment, code sign-in, last-factor protection, adding a preferred passkey from a TOTP session, alternate sign-in methods and actual WebAuthn verification. No console errors. Test codes are generated from synthetic secrets using PyOTP; no real Google account/phone is involved.
- Existing passkey/account harness checks opt-out default behavior, passkey enrollment/recovery, invitations, membership isolation and SSE revocation. Frontend production build/lint and generated OpenAPI/types are checked.
- API/auth dependency audit: 31 resolved dependencies, no known vulnerabilities reported by the advisory service. The explicit cryptography pin matches the audited/tested 50.0.2 runtime. OS/FFmpeg/inference/image-wide and JavaScript dependency qualification are separate public-release gates.

Local ignored evidence is under `.recordings/totp-*`; synthetic secrets/data are excluded from Git. No real administrator secret was created or server configuration changed. The public worker/data isolation, upload-abuse, HTTPS/gateway and real-Philemon gates in [the ingress draft](PUBLIC-ACCESS-DRAFT.md) remain open.

Primary implementation references: [PyOTP security guidance](https://pyauth.github.io/pyotp/), [Fernet key custody](https://cryptography.io/en/latest/fernet/), [Segno local QR generation](https://segno.readthedocs.io/).
