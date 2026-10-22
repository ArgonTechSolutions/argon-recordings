# Team edition and public hosting plan

Status: proposed architecture. Public exposure, identity-provider configuration, SMTP setup, and production migration have not been performed. The current application remains a local single-user profile until these milestones are implemented and accepted.

## Product and deployment contract

Ship an invitation-only application with a dedicated administrator frontend, local accounts, and optional standards-based OpenID Connect (OIDC) sign-in from the first team release. No external identity provider is mandatory. Instance administrators manage the installation; project membership controls content access independently.

Keep installation behind the existing launcher and configuration file. The public profile packages the API and resource-limited inference/media worker as separate services, with a curated HTTPS gateway configuration when an existing gateway is unavailable. The worker has no mount of the account/session database. Public ingress never forwards worker endpoints. Keep model files shared read-only, preserve named data volumes and installation identity, and bound aggregate CPU/RAM rather than granting each service the old aggregate budget. One active heavy job remains the default. SQLite and one API process remain sufficient for the initial small-team target; API replicas require a shared event broker first.

A standalone local-preview profile remains available. Team mode never falls back to anonymous access if authentication configuration is missing or broken.

## Identity, invitations and sessions

- Stable application user IDs, separate local password credentials, external identities uniquely keyed by issuer/subject, sessions, invitations, recovery challenges, and audit events. Project membership references the stable user ID.
- Local email/password accounts use Argon2id through a maintained library. Implement OIDC through a maintained client library (Authlib is the proposed Python integration), authorization code flow, PKCE, state/nonce and strict issuer/audience/redirect checks. Do not write protocol or cryptographic primitives ourselves.
- SSO sign-in does not bypass invitation policy. Do not automatically merge accounts based on matching email addresses; linking requires authenticated confirmation. Retain only provider tokens actually needed for sign-in.
- Server-side opaque sessions, hashed bearer values, Secure/HttpOnly/SameSite cookies in public mode, CSRF protection, expiration, session rotation and revocation. Session and identity data live in the protected existing data volume. Do not expose tokens through URLs or browser local storage.
- Require strong administrator authentication (a passkey or MFA), recent reauthentication for sensitive actions, recovery codes, and an explicit terminal recovery path. Ordinary accounts may enroll strong authentication too. Fix the production hostname before passkey enrollment.
- Bootstrap the initial administrator through an interactive terminal flow, never through a first-public-visitor claim or a default password. Protect the last active administrator and project ownership handovers.

## Administrator frontend

Build a dedicated entrypoint using the existing React/shadcn design system, with a separate administrative session audience and strictly authorized `/api/admin` routes in the API. The API code separates identity, administration, permissions and project operations into explicit modules. This is not a claim of process isolation; independent admin-service deployment is a later option if operational isolation is required.

Support users, invitations, account suspension, session revocation, roles, SMTP status/test delivery, authentication providers, and registration policies. Initially expose closed and invitation-only policies; policy storage must accommodate later approved-domain or moderated registration without changing identity tables. Open registration requires its own abuse controls and is not enabled merely by adding a selector.

Prefer private administrative access even when the user application is public. Public administration requires strong authentication and the same security acceptance gates. Do not show model/deployment details in ordinary recording workflows.

## Project authorization

Retain Owner, Editor and Viewer roles. Viewers can read/search/download accessible project content; editors additionally upload, retry, and propose/accept corrections; owners manage membership and ownership. New projects are private to their creator. Instance-admin status is not implicit content membership. Operator-assisted ownership recovery is explicit and audited.

Centralize checks for every list, direct object lookup, media/range download, search, correction and event route. Scope database and vector retrieval before exposing results. Browser caches must be cleared on account changes. Worker service authentication remains separate from browser authentication.

Authorize SSE subscription and delivery. Revoke streams and discard queued snapshots when sessions or memberships are revoked; recheck expiry before delivery. Shared project snapshots never grant subscription permission on their own.

## Email and secrets

SMTP is optional for basic installation and recommended for public operation. Deliver invitations, email verification, local-password resets and security notices from a small SQLite-backed outbox with bounded retries/timeouts. Failed email delivery must not block API threads or inference. Preserve manually shareable invitation links for installations without SMTP.

Provision SMTP/provider credentials through a private setup flow into an operator-owned protected secret file or secret store outside Git, image layers and logs. Configuration references secrets rather than embedding them. Confirm the concrete storage and custody mechanism before generating or saving production secrets. Audit configuration changes without recording secret values. Do not request SMTP passwords in chat.

## Public ingress and uploads

The recommended operator-controlled topology is public HTTPS gateway -> restricted private tailnet route -> application API. Only the designated gateway may reach the origin listener. Configure exact public origins, trusted proxy hops, CSRF rules, security headers, upload limits/timeouts, SSE buffering exceptions and private response caching explicitly. Public deployment is a separate reviewed operation.

Cloudflare Tunnel is another supported topology, but its HTTP proxy request-size limits matter for large recordings. Add resumable uploads using a documented protocol (evaluate tus) with bounded chunk requests, final size/hash validation, interrupted-upload recovery, storage reservation and per-user/project quotas. These controls also improve slow connections through a private gateway. Keep any single-request upload compatibility path under the same authorization and quota rules.

Treat media parsing as an untrusted-input boundary. Isolate decoding from account/session storage, restrict worker network/filesystem access, and test malformed media and resource exhaustion. Preserve the worker lease fence and existing artifact publication checks.

## Incremental delivery and acceptance

1. Establish the team schema and explicit migration/backup/restore contract; implement stable identities, sessions, local sign-in, OIDC adapters, invitations, strong admin authentication and terminal bootstrap. Validate with a disposable OIDC provider; do not require an external provider for installation.
2. Enforce project permissions across all existing routes, including immediate SSE revocation and user-switch cache cleanup. Add the dedicated admin frontend, membership UI, SMTP outbox and audited policy management.
3. Package the public deployment profile and resumable uploads. Qualify HTTPS/proxy behavior, worker isolation, quotas, retries and upgrade paths without opening a real public endpoint.
4. Review and apply the existing-data migration explicitly: stop processing, back up consistent database/files, assign existing projects to a named administrator, validate counts/ownership, and preserve original/model/data volumes. Rollback must restore matching schema and source versions.
5. Run the real-host acceptance and separately approve public exposure. Test several simultaneous users with one active Turbo job, login/search latency, aggregate CPU/RAM, upload resume, SSE reconnect/revocation, email failures, backup/restore, and restart/update behavior.

Security acceptance includes two users with disjoint projects, direct-ID guessing on every route, authorization before upload body consumption, invitation/reset replay, session fixation/CSRF, OIDC replay and account-linking attacks, cross-project search/media/event leakage, and administrator privilege boundaries. Preserve OpenAPI, frontend build/lint and bounded browser checks; commission a focused security review for the new public authentication and upload boundaries before exposure.

## Primary references

- [Authlib web/OIDC clients](https://docs.authlib.org/en/latest/oauth2/client/web/)
- [OWASP OAuth security](https://cheatsheetseries.owasp.org/cheatsheets/OAuth2_Cheat_Sheet.html)
- [OWASP file uploads](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)
- [Cloudflare request-size limits](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/)
- [Tailscale Funnel limitations](https://tailscale.com/docs/features/tailscale-funnel)
