import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { request } from "@/lib/api"
import { setSessionTransport, type AccountSession } from "@/lib/account-session"
import { performPasskey } from "@/lib/passkeys"
import { Button } from "@/components/ui/button"
import { AuthenticatorApp } from "./authenticator-app"
import { Input } from "@/components/ui/input"

type Status = {
  verified: boolean
  method: string | null
  rp: string
  keys: { id: string; name: string }[]
  totp_allowed: boolean
  totp_enrolled: boolean
  recovery_remaining: number
}
const message = (error: unknown) =>
  error instanceof DOMException && error.name === "NotAllowedError"
    ? "Passkey request was cancelled or timed out. Try again."
    : error instanceof Error
      ? error.message
      : "Please try again."

export function AdminSecurity({
  locked = false,
  onEnrollment,
  onVerified,
}: {
  locked?: boolean
  onEnrollment?: () => void
  onVerified?: () => Promise<unknown>
}) {
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [codes, setCodes] = useState<string[] | null>(null)
  const [authenticator, setAuthenticator] = useState<"setup" | "verify" | null>(
    null
  )
  const [recovery, setRecovery] = useState(false)
  const status = useQuery({
    queryKey: ["admin-mfa"],
    queryFn: () => request<Status>("/api/auth/mfa"),
    retry: false,
  })
  async function act(action: () => Promise<unknown>, refresh = false) {
    setError("")
    setBusy(true)
    try {
      await action()
      if (refresh) {
        await status.refetch()
        await onVerified?.()
      }
    } catch (error) {
      setError(message(error))
    } finally {
      setBusy(false)
    }
  }
  async function completed(result: { recovery_codes?: string[] }) {
    if (result.recovery_codes) setCodes(result.recovery_codes)
    const session = await request<AccountSession>(
      "/api/auth/session?audience=admin"
    )
    setSessionTransport(session, "admin")
    await status.refetch()
    if (!result.recovery_codes) await onVerified?.()
  }
  async function passkey(register: boolean, form?: FormData) {
    if (register) onEnrollment?.()
    const result = await performPasskey(
      register,
      String(form?.get("code") ?? ""),
      String(form?.get("name") ?? "Passkey")
    )
    await completed(result)
  }

  if (status.isPending) return <p>Loading security settings…</p>
  if (status.isError)
    return (
      <div>
        <p role="alert">{message(status.error)}</p>
        <Button onClick={() => status.refetch()}>Try again</Button>
      </div>
    )
  const value = status.data!
  if (codes)
    return (
      <section className="account-form">
        <h2>Save your recovery codes</h2>
        <p>
          Each code works once after sign-in. Save them in your password
          manager; they will not be shown again.
        </p>
        <div className="recovery-codes">
          {codes.map((code) => (
            <code key={code}>{code}</code>
          ))}
        </div>
        <Button
          onClick={() => {
            setCodes(null)
            void onVerified?.()
          }}
        >
          I saved my codes
        </Button>
      </section>
    )
  const enrolled = value.keys.length > 0
  const hasFactor = enrolled || value.totp_enrolled
  if (authenticator)
    return (
      <section className={locked ? "account-form" : "admin-card"}>
        <AuthenticatorApp
          setup={authenticator === "setup"}
          needsEnrollmentCode={!hasFactor}
          onEnrollment={onEnrollment}
          onReverify={enrolled ? () => passkey(false) : undefined}
          onComplete={async (result) => {
            setAuthenticator(null)
            await completed(result)
          }}
          onCancel={() => setAuthenticator(null)}
        />
        {locked && (
          <Button
            variant="ghost"
            onClick={() =>
              act(async () => {
                await request("/api/auth/logout", { method: "POST" })
                location.reload()
              })
            }
          >
            Sign out
          </Button>
        )}
      </section>
    )
  return (
    <section className={locked ? "account-form" : "admin-card"}>
      <h2>
        {locked
          ? hasFactor
            ? "Verify your identity"
            : "Set up administrator security"
          : "Administrator security"}
      </h2>
      {locked && enrolled && (
        <Button disabled={busy} onClick={() => act(() => passkey(false))}>
          Continue with passkey
        </Button>
      )}
      {(!locked || !hasFactor) && (
        <form
          className="account-form"
          onSubmit={(event) => {
            event.preventDefault()
            const form = new FormData(event.currentTarget)
            void act(() => passkey(true, form))
          }}
        >
          {!hasFactor && (
            <>
              <p>
                Ask the host operator to run{" "}
                <code>python3 recordings.py setup-admin-mfa</code>, then enter
                the one-time code.
              </p>
              <label>
                Enrollment code
                <Input
                  name="code"
                  type="password"
                  autoComplete="off"
                  required
                  maxLength={200}
                />
              </label>
            </>
          )}
          <label>
            Passkey name
            <Input
              name="name"
              placeholder="My laptop"
              required
              maxLength={80}
            />
          </label>
          <Button disabled={busy} type="submit">
            {enrolled ? "Add passkey" : "Create passkey"}
          </Button>
        </form>
      )}
      {value.totp_allowed &&
        (!value.totp_enrolled || locked) &&
        (!locked || value.totp_enrolled || !hasFactor) && (
          <Button
            variant={locked && !enrolled ? "default" : "outline"}
            disabled={busy}
            onClick={() =>
              setAuthenticator(
                value.totp_enrolled && locked ? "verify" : "setup"
              )
            }
          >
            {value.totp_enrolled && locked
              ? "Use authenticator code"
              : !value.totp_enrolled
                ? "Set up authenticator app"
                : ""}
          </Button>
        )}
      {!locked && value.totp_enrolled && (
        <div className="admin-row">
          <span>Authenticator app enabled</span>
          <Button
            variant="outline"
            disabled={busy || !enrolled}
            onClick={() => {
              if (
                window.confirm(
                  "Remove your authenticator app? You will need to sign in again."
                )
              )
                void act(async () => {
                  await request("/api/auth/mfa/totp", { method: "DELETE" })
                  location.reload()
                })
            }}
          >
            Remove authenticator app
          </Button>
        </div>
      )}
      {locked && hasFactor && (
        <>
          <Button
            variant="ghost"
            disabled={busy}
            onClick={() => setRecovery(!recovery)}
          >
            {recovery ? "Use a passkey instead" : "Use a recovery code"}
          </Button>
          {recovery && (
            <form
              className="account-form"
              onSubmit={(event) => {
                event.preventDefault()
                const form = new FormData(event.currentTarget)
                void act(
                  () =>
                    request("/api/auth/mfa/recovery", {
                      method: "POST",
                      body: JSON.stringify({ code: form.get("code") }),
                    }),
                  true
                )
              }}
            >
              <label>
                Recovery code
                <Input
                  name="code"
                  type="password"
                  autoComplete="off"
                  required
                  minLength={20}
                  maxLength={100}
                />
              </label>
              <Button type="submit" disabled={busy}>
                Verify recovery code
              </Button>
            </form>
          )}
        </>
      )}
      {!locked && (
        <>
          {value.keys.map((key) => (
            <div className="admin-row" key={key.id}>
              <span>{key.name}</span>
              <Button
                variant="outline"
                disabled={busy || value.keys.length < 2}
                onClick={() => {
                  if (window.confirm(`Remove ${key.name}?`))
                    void act(
                      () =>
                        request(
                          `/api/auth/mfa/keys/${encodeURIComponent(key.id)}`,
                          { method: "DELETE" }
                        ),
                      true
                    )
                }}
              >
                Remove
              </Button>
            </div>
          ))}
          <p>{value.recovery_remaining} recovery codes remaining.</p>
          {(value.method === "passkey" ||
            (value.method === "totp" && value.totp_allowed)) && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => {
                if (
                  window.confirm(
                    "Replace your recovery codes? All previous codes will stop working."
                  )
                )
                  void act(async () => {
                    const result = await request<{ codes: string[] }>(
                      "/api/auth/mfa/recovery-codes",
                      { method: "POST" }
                    )
                    setCodes(result.codes)
                  })
              }}
            >
              Replace recovery codes
            </Button>
          )}
        </>
      )}
      {busy && <p role="status">Waiting for verification…</p>}
      {error && <p role="alert">{error}</p>}
      {locked && (
        <Button
          variant="ghost"
          disabled={busy}
          onClick={() =>
            act(async () => {
              await request("/api/auth/logout", { method: "POST" })
              location.reload()
            })
          }
        >
          Sign out
        </Button>
      )}
    </section>
  )
}
