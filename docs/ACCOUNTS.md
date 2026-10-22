# Accounts and instance administration

This slice adds local accounts, invitation-only or closed registration, optional OpenID Connect, project roles and a separate `/admin/` frontend. It does not publish the app. Public ingress, SMTP delivery, resumable uploads, and API/worker isolation remain release gates in [the team plan](TEAM-EDITION-PLAN.md). Keep the current deployment private until those gates and a focused security review are complete.

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

## Administrator passkeys and recovery

Administration requires a second factor after local-password or OIDC sign-in. Passkeys remain preferred; an optional installation policy can also allow Google Authenticator-compatible TOTP codes. Password-only admin sessions cannot read or change `/api/admin/*`. WebAuthn requires authenticator user verification (PIN/biometric), an exact RP/origin and a short-lived server challenge. The app does not retain biometric data or authenticator private keys. App sign-in for recording access is unchanged.

After updating, existing administrators must enroll before using the admin panel. Existing app sessions, accounts, recordings and model directories remain intact; schema 2 adds passkey tables/session proof fields and schema 3 adds encrypted authenticator-app factors and pending enrollments. Old admin sessions have no proof. Back up the consistent data volume before updating; rollback needs matching source/database versions.

In the host terminal, run:

```bash
python3 recordings.py setup-admin-mfa
```

Enter the active administrator email. The CLI displays a one-time enrollment code, valid for ten minutes, for that administrator and configured hostname. Keep it private. Sign in at `/admin/`, enter the code and a passkey name, then create the passkey using your browser/password manager/security key. First enrollment needs both the authenticated administrator and the host-issued code; knowing the password alone cannot claim the first passkey. New enrollments require authenticator user verification and consume the code atomically only when successful. Challenges expire after two minutes and are bound to the exact admin session, operation and origin; retries issue a new challenge.

Save the eight recovery codes shown after enrollment in a password manager. They are displayed once and stored only as hashes. Each restores admin access once after normal password/OIDC sign-in. A recovery session may add a replacement passkey for a lost device; doing so rotates all remaining recovery codes and revokes other admin sessions. It cannot remove existing passkeys or replace recovery codes until a passkey is verified. A passkey-verified recent session can add keys, replace recovery codes and remove keys, with at least one key retained per hostname. Up to eight keys per administrator/hostname are allowed. Credential changes are audited without recording codes or credential material.

Successful verification rotates the admin cookie and gives a one-hour admin session; sensitive writes require proof within 15 minutes. App sessions remain separate. No claim is made that ordinary app sessions have MFA. Password resets do not remove passkeys. For lost passwords use `recover-admin`; for loss of all passkeys and recovery codes the host operator can use:

```bash
python3 recordings.py recover-admin-mfa
```

This asks for the administrator email and an explicit `RESET` confirmation, removes that administrator's passkeys, authenticator apps, pending enrollments and recovery codes, revokes all their sessions, and issues a new short-lived enrollment code. It does not alter recordings, models or memberships. No browser administrator can reset another administrator's MFA. The host operator retains the explicit recovery authority.

For SSH-tunnel testing browse `http://localhost:8787`, not a remote IP or `127.0.0.1`. Public operation requires the final HTTPS `auth.public_url` before enrollment. Localhost and public-hostname passkeys are different; changing the hostname requires a new operator-authorized enrollment for that hostname. OIDC remains subject to the same admin second factor, regardless of upstream MFA claims. See the [Forculus/Tailscale draft](PUBLIC-ACCESS-DRAFT.md) before planning public exposure.

## Optional Google Authenticator support

TOTP is an administrator second-factor alternative, compatible with Google Authenticator and other RFC 6238 apps. It is disabled by default and requires no Google account, SMTP or external authentication service. Passkeys stay the preferred sign-in button when enrolled; TOTP codes are phishable and enabling this policy permits that lower-assurance alternative.

To enable it, update through the normal launcher after a consistent data backup, then edit the existing `[auth]` section in `recordings.toml`:

```toml
[auth]
mode = "accounts"
allow_totp = true
# Keep your existing public_url, if configured.
```

Restart to apply the policy:

```bash
python3 recordings.py stop
python3 recordings.py start
```

Use `sudo -v` first on installations with `docker.sudo=true`. No model downloads, additional services or manually managed encryption environment variables are required.

An administrator with recent passkey verification opens **Passkeys and recovery → Set up authenticator app**. Scan the locally generated QR code with Google Authenticator, or copy the manual setup key, then confirm a six-digit code. Pending setup expires after ten minutes and grants no administrative access until confirmation. The setup key/QR is displayed only for that enrollment and retained only in component memory. Cancel/navigation/confirmation clear that UI state. Do not share screenshots of enrollment or recovery codes.

An administrator with no factor can choose **Set up authenticator app** at the verification screen instead. The host operator must first run `python3 recordings.py setup-admin-mfa` and privately supply that administrator's ten-minute enrollment code. First enrollment needs both this code and the signed-in administrator session. Existing password/OIDC sign-in alone cannot enroll a factor. The same enrollment ticket cannot complete both TOTP and passkey enrollment.

On later sign-ins choose **Use authenticator code**. Codes have six digits and a 30-second step; the server permits one adjacent step in either direction for clock skew. Successful codes, including the enrollment confirmation code, cannot be reused. If a code was just used, wait for the next one. Keep Philemon's system clock synchronized. Verification attempts are rate-limited per administrator and successful verification rotates the admin cookie.

TOTP-verified sessions may administer the instance, add a passkey and replace recovery codes. Recovery-code sessions cannot remove factors or replace recovery codes until an actual authenticator is verified. Removing a passkey still requires passkey verification and retains the last passkey. Removing an authenticator app requires a recent passkey/TOTP verification and at least one passkey at that hostname; removal revokes all admin sessions for that account. First TOTP enrollment and replacement following recovery rotate recovery codes and revoke other admin sessions. Password reset does not disable MFA; `recover-admin-mfa` remains the explicit terminal recovery path for loss of all factors.

Set `allow_totp=false` and restart to disable this alternative. TOTP-verified sessions are deleted, not merely hidden until re-enablement. The encrypted enrollment is retained so enabling the policy later does not require rescanning, but a fresh sign-in is required. Have a passkey or terminal recovery path available before disabling TOTP on a TOTP-only account. This toggle does not alter recording/model files or app sessions.

TOTP requires a recoverable shared secret, unlike passkeys. SQLite stores authenticated encrypted ciphertext bound to account and hostname. The app automatically creates `/data/auth/totp.key` with mode `0600` on first authorized setup and retains it across restarts. Back up the entire existing data volume, including that key and the matching SQLite database; keep backups private. A missing, corrupt, mismatched or insecurely permissioned key fails closed for TOTP and is not silently regenerated while encrypted factors exist. Restore the matching key/database or use operator MFA recovery. Encryption protects a database-only leak; it does not protect against an attacker controlling the host or the entire data volume. The existing public worker-isolation release gate still applies.

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
