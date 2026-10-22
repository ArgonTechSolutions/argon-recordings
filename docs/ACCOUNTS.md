# Accounts and instance administration

This slice adds local accounts, invitation-only or closed registration, optional OpenID Connect, project roles and a separate `/admin/` frontend. It does not publish the app. Public ingress, SMTP delivery, administrator MFA/passkeys, resumable uploads, and API/worker isolation remain release gates in [the team plan](TEAM-EDITION-PLAN.md). Keep the current deployment private until those gates and a focused security review are complete.

## Installation

New `recordings.py init` configurations select account mode. Continue using the normal install/up commands; the launcher still binds Docker only to `127.0.0.1` and keeps the same model/data volumes. After the app starts, create its first administrator in an interactive terminal:

```bash
python3 recordings.py setup-admin
```

The command prompts privately for the email, display name and password. Passwords must contain 15–128 characters. There are no default credentials or browser-based first-owner claims. Accounts and hashed sessions are stored in the existing private SQLite data volume. Passwords use Argon2id with a single bounded verification slot. Administrator sessions are separate from app sessions, last one hour, and require a fresh sign-in for administrative changes after 15 minutes. App sessions last 12 hours. Logout/password reset/suspension revoke sessions; project membership is checked server-side on each request and SSE delivery.

Open `/` for recordings and `/admin/` for administration. Sign in separately to administration. Use its invitation form to generate a one-time link valid for 24 hours. Share that link privately with the intended user. The token is in the URL fragment, not the server request URL. The link is only displayed when issued; stored invitation tokens are hashed. SMTP is not required for this workflow. Administrator-issued password reset links expire after 30 minutes. Closing registration prevents outstanding invitations from being accepted; existing users can still sign in.

Project owners manage members by an existing account's exact email, selecting Viewer, Editor or Owner. New projects belong only to their creator. Instance-administrator status does not automatically grant project-content access. Transfer ownership before removing the last owner or suspending a sole owner. At least one active instance administrator must remain. The account menu supports password changes, explicit SSO linking, and signing out all devices.

## Existing installations

Configurations without `[auth]` preserve the old private local-preview profile. Updating source does not silently migrate ownership. Before enabling accounts, back up a consistent copy of the data volume and wait for processing to finish. Add:

```toml
[auth]
mode = "accounts"
```

Stop/start the app with the usual launcher commands, then run the following **only after reviewing assignment of all existing projects to the initial administrator**:

```bash
python3 recordings.py setup-admin --adopt-existing
```

Adoption is transactional: it creates the initial administrator, assigns existing projects, and marks authentication activated. It refuses an active transcription lease or an existing account. Recording/model files are not rewritten. Once activated, this code refuses startup in anonymous mode; do not downgrade to earlier anonymous-only releases against an account-enabled database. Rollback requires a matching pre-upgrade database/source backup. No real-data adoption has been performed by development tests.

If an administrator loses access, the host operator can run `python3 recordings.py recover-admin` in a private terminal. It prompts for an existing administrator email and new password, revokes that user's sessions, and records an operator-recovery audit event. It cannot silently create a new administrator.

## OIDC setup

OIDC is optional. A stable HTTPS URL and an existing identity provider/application registration are required; the app does not install an identity server. Configure the final browser origin before setup:

```toml
[auth]
mode = "accounts"
public_url = "https://recordings.example.com"
```

This URL controls origin checks and Secure cookies; it does not configure DNS, TLS, proxying or public exposure. After restarting with this configuration, local plain-HTTP browser access will no longer carry those Secure session cookies. Use the configured HTTPS route for sign-in. Keep that route private while qualifying this slice.

Run `python3 recordings.py setup-oidc` in a private terminal. Supply the provider's HTTPS issuer, client ID and client secret through the prompts. The secret is saved with mode `0600` at `/data/auth/oidc.json` inside the existing private Docker data volume, never in TOML, Git, command arguments or Docker environment metadata. Protect this file in backups. Register this exact callback with the provider:

```text
https://recordings.example.com/api/auth/oidc/callback
```

The app uses Authlib's authorization-code flow, PKCE, browser-bound one-time state, nonce, issuer/audience/time and signed ID-token validation. Existing local accounts explicitly link SSO from their account menu after a recent local sign-in. Matching email addresses never silently merge identities. A new external identity must accept an invitation and supply the invitation's verified provider email. SSO does not bypass closed/invitation-only registration. External identities are bound to issuer and subject, independently of project memberships.

Keep provider endpoints HTTPS and use a confidential OIDC client supporting authorization code with S256 PKCE and RS256, ES256 or PS256 ID-token signing. Failed or replayed callbacks return a generic sign-in error; a failed exchange must start a new flow. Provider credentials can be replaced by rerunning the private setup command; account identities remain bound to the issuer/subject pair.

## Validation and limits

Tests cover local login/logout, CSRF, hashed storage, one-time invitation/reset replay, rate limiting, account suspension, ownership protection, cross-project access to search/media/corrections/uploads/events, actual Authlib signed-token checks, and live SSE revocation. Disposable browser tests exercise separate app/admin login, invitation acceptance, project-membership UI, viewer restrictions, policy changes, suspension without reloading, and mobile layouts. These are local test results, not a public deployment or a live-provider interoperability claim.

The account/admin JSON request budget is 64 KiB. Verification is bounded to one Argon2 operation at a time. Session retention is bounded per user/audience; expired challenge/rate-limit entries are pruned. The current account flow uses manually shared links; it does not send email or require an SMTP account. Account role changes are audited without recording passwords, session tokens, invitation tokens or OIDC client secrets. Keep one API process: the shared in-memory event broker cannot relay writes from multiple API replicas.
