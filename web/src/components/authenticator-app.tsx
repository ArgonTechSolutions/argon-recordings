import { useState } from "react"
import { ApiError, request } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

type Setup = { secret: string; qr: string }
export function AuthenticatorApp({
  setup,
  needsEnrollmentCode = false,
  onEnrollment,
  onComplete,
  onCancel,
  onReverify,
}: {
  setup: boolean
  needsEnrollmentCode?: boolean
  onEnrollment?: () => void
  onComplete: (result: { recovery_codes?: string[] }) => Promise<unknown>
  onCancel: () => void
  onReverify?: () => Promise<void>
}) {
  const [provision, setProvision] = useState<Setup | null>(null)
  const [busy, setBusy] = useState(false)
  const [needsReverification, setNeedsReverification] = useState(false)
  const [error, setError] = useState("")
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setBusy(true)
    setError("")
    try {
      if (setup && !provision) {
        onEnrollment?.()
        setProvision(
          await request<Setup>("/api/auth/mfa/totp/setup", {
            method: "POST",
            body: JSON.stringify({ code: form.get("enrollment") ?? "" }),
          })
        )
      } else {
        const result = await request<{ recovery_codes?: string[] }>(
          `/api/auth/mfa/totp/${setup ? "confirm" : "verify"}`,
          {
            method: "POST",
            body: JSON.stringify({ code: form.get("otp") }),
          }
        )
        setProvision(null)
        await onComplete(result)
      }
    } catch (error) {
      setNeedsReverification(
        error instanceof ApiError && error.code === "reauthentication_required"
      )
      setError(error instanceof Error ? error.message : "Please try again.")
    } finally {
      setBusy(false)
    }
  }
  return (
    <form className="account-form" onSubmit={submit}>
      <h3>{setup ? "Set up authenticator app" : "Authenticator code"}</h3>
      {setup && !provision && (
        <>
          <p>Use Google Authenticator or another compatible app.</p>
          {needsEnrollmentCode && (
            <label>
              Enrollment code
              <Input
                name="enrollment"
                type="password"
                autoComplete="off"
                required
                maxLength={200}
              />
            </label>
          )}
        </>
      )}
      {provision && (
        <>
          <p>Scan this QR code, then enter the six-digit code from your app.</p>
          <img
            className="authenticator-qr"
            src={provision.qr}
            alt="Scan to add Argon Recordings to your authenticator app"
          />
          <details>
            <summary>Can’t scan the code?</summary>
            <label>
              Setup key
              <Input
                value={provision.secret}
                readOnly
                autoComplete="off"
                onFocus={(event) => event.target.select()}
              />
            </label>
          </details>
        </>
      )}
      {(!setup || provision) && (
        <label>
          Six-digit code
          <Input
            key="otp"
            name="otp"
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="[0-9]{6}"
            minLength={6}
            maxLength={6}
            required
          />
        </label>
      )}
      <Button type="submit" disabled={busy}>
        {busy
          ? "Please wait…"
          : setup && !provision
            ? "Show QR code"
            : "Verify code"}
      </Button>
      <Button
        variant="ghost"
        type="button"
        disabled={busy}
        onClick={async () => {
          setProvision(null)
          if (setup)
            await request("/api/auth/mfa/totp/setup", {
              method: "DELETE",
            }).catch(() => undefined)
          onCancel()
        }}
      >
        Cancel
      </Button>
      {error && <p role="alert">{error}</p>}
      {needsReverification && onReverify && (
        <Button
          type="button"
          variant="outline"
          disabled={busy}
          onClick={async () => {
            setBusy(true)
            try {
              await onReverify()
              setProvision(null)
              setNeedsReverification(false)
              setError("")
            } catch (error) {
              setError(
                error instanceof Error
                  ? error.message
                  : "Verification could not be completed. Try again."
              )
            } finally {
              setBusy(false)
            }
          }}
        >
          Verify passkey again
        </Button>
      )}
    </form>
  )
}
