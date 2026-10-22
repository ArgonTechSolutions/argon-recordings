export type Account = { id: string; email: string; name: string; admin: number }
export type AccountSession = {
  enabled: boolean
  user: Account | null
  csrf: string | null
  oidc: boolean
  mfa_verified?: boolean
  registration?: "invite" | "closed"
}
let csrfToken: string | null = null
let audience: "app" | "admin" = "app"
export function setSessionTransport(
  value: AccountSession,
  target: "app" | "admin"
) {
  csrfToken = value.csrf
  audience = target
}
export function accountHeaders(): Record<string, string> {
  return {
    "X-Session-Audience": audience,
    ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
  }
}
