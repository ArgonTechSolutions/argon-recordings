# Public access: Forculus and Tailscale (draft)

Status: design only. No DNS, firewall, Tailscale, gateway, server or production configuration has been changed. Administrator passkeys are one prerequisite; this document does not authorize public exposure. Argon owns the application; the project KB article in YouTrack is pending.

## Proposed route

```text
Browser -> HTTPS on Forculus -> restricted Tailscale TCP route
        -> Philemon loopback app listener -> account/project authorization
```

Use a stable application hostname chosen by the operator, for example `recordings.example.com`. Forculus terminates public TLS and forwards only application traffic. Philemon keeps its existing `127.0.0.1:8787` Docker publication. A separate tailnet-only TCP forwarder, preferably Tailscale Serve, connects the approved origin port to that loopback listener. This avoids opening the Docker port on LAN/public interfaces. Serve supports TCP forwarding to localhost, and tailnet access rules still apply ([CLI reference](https://tailscale.com/docs/reference/tailscale-cli/serve), [access controls](https://tailscale.com/docs/features/tailscale-serve)). Do not use Funnel for this topology.

Allow only Forculus's managed tailnet identity to reach the designated origin port. Review the effective tailnet policy, including broad pre-existing grants: adding a narrow grant does not subtract another broad grant. Deny other tailnet clients and verify that denial. A local listener alone does not establish isolation; test the complete network path. TCP forwarding carries the original application Host without adding another HTTP origin or trusting Tailscale identity headers as application authentication.

Keep administration private initially. The preferred design uses the same HTTPS application hostname through a private Forculus listener/split DNS for administrator access, with public ingress denying `/admin`, `/admin/`, `/api/admin/*` and `/api/auth/mfa*`. Review normalized/encoded paths and path aliases to prevent policy bypass. The private and public routes must resolve to the same application and RP hostname, rather than accidentally creating two passkey identities. General auth endpoints still enforce audience/role and MFA server-side; hiding the admin frontend alone is not authorization. Confirm this routing design before writing operator-specific configuration.

## Browser origin, sessions and passkeys

Set `auth.public_url` to the exact final HTTPS origin only when the HTTPS path works privately. It enables Secure cookies and exact passkey origin/RP verification; it does not configure TLS or publish a port. Do not expand allowed origins using arbitrary forwarded headers. Forculus must set the expected Host, overwrite forwarded metadata, and have a defined trusted-proxy policy; the bundled service currently disables Uvicorn proxy-header trust.

Local testing uses `http://localhost:<tunnel-port>`. Credentials enrolled for localhost cannot authenticate at the final hostname. Before cutover, issue a new terminal enrollment code for the administrator after configuring the final origin, then enroll and verify a key there through the private HTTPS route. Keep the password, backup and terminal recovery route available. Cookies and keys must not silently fall back to the old HTTP origin after cutover. Passkey origin rules come from [WebAuthn](https://www.w3.org/TR/webauthn-3/).

## Gateway behavior to qualify

- Deny `/api/worker/*` externally on both public and administrative gateway routes. Browser identity is never a worker credential. Limit methods and paths before body forwarding.
- Apply TLS, exact Host admission, private-response `no-store`, `nosniff`, frame restrictions, a tested CSP, referrer policy and HSTS after HTTPS qualification. Keep cookies host-only; assess `__Host-` names before public release to resist sibling-subdomain cookie injection.
- Turn off response buffering for authenticated SSE, allow heartbeats and reconnects, and avoid retrying non-idempotent requests. Test session/membership revocation during an open stream. NGINX documents these buffering controls in its [proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).
- Bound uploads at gateway and app consistently; bound buffering/temp-disk use and stalled request timeouts. Qualify resumable uploads, aggregate reservations and per-user/project quotas before public large-video use. The current single-request upload cap alone does not provide these protections.
- Keep shared project/media/search responses out of gateway caches. Preserve range requests and download authorization. Rate-limit authentication at the edge without trusting client-supplied IP headers. The current application sees the proxy peer, so its IP rate limit aggregates users until the trusted-proxy design is implemented.
- Strip credentials, request bodies, invitation/reset URLs and query values from logs. Define bounded retention. Do not log passkey challenges, enrollment or recovery codes.

## Blocking work before exposure

1. Qualify the isolated worker profile on Philemon. The installer now separates API/account storage from media workers, with per-worker RAM-backed credentials, private work volumes, restricted worker endpoints and aggregate resource limits (see [WORKERS.md](WORKERS.md)). Local/disposable checks do not establish hostile-media containment or real-host acceptance.
2. Qualify hostile media, upload reservations/quotas, resume/interruption, per-user abuse and concurrency. Preserve one heavy job by default; a public listener must not turn a queue into unbounded storage/CPU use.
3. Implement and test the gateway/proxy security headers, credential custody, backup/restore and monitoring path. Resolve applicable dependency advisories in the packaged API, worker and media runtime before release.
4. Test the final HTTPS origin and real user authenticators on Philemon; test lost-passkey/recovery/host recovery and origin cutover. Automated virtual-authenticator evidence is not real-device acceptance.
5. Perform a focused review of the finished public profile, then request explicit approval for exact deployment targets/configuration and public exposure.

## Operator inputs for the deployment slice

Choose the final hostname, identify the existing Forculus gateway owner/service and configuration entrypoint, confirm Forculus and Philemon tailnet enrollment and managed identities, and approve the administration routing policy. Inspect those hosts read-only before producing a concrete migration/rollback runbook. No credentials are needed in chat. SMTP and external OIDC remain optional and can be provisioned separately through private setup flows.
