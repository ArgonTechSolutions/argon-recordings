import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { request, preview } from "@/lib/api"
import { setSessionTransport, type AccountSession } from "@/lib/account-session"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ValueSelect } from "@/components/value-select"
import { AdminSecurity } from "./admin-security"
import "./accounts.css"

const Session = createContext<AccountSession | null>(null)
export const useAccountSession = () => useContext(Session)
const message = (error: unknown) =>
  error instanceof Error ? error.message : "Something went wrong"

export function AuthGate({
  children,
  audience = "app",
}: {
  children: ReactNode
  audience?: "app" | "admin"
}) {
  const cache = useQueryClient()
  const previous = useRef<string | null | undefined>(undefined)
  const [accessGeneration, setAccessGeneration] = useState(0)
  const [error, setError] = useState(
    new URLSearchParams(location.search).has("auth_error")
      ? "Single sign-on could not be completed. Try again."
      : ""
  )
  const [pending, setPending] = useState(false)
  const [enrolling, setEnrolling] = useState(false)
  const session = useQuery({
    queryKey: ["account-session", audience],
    enabled: !preview,
    queryFn: async () => {
      const value = await request<AccountSession>(
        `/api/auth/session?audience=${audience}`
      )
      const identity = value.user?.id ?? null
      if (previous.current !== undefined && previous.current !== identity) {
        cache.removeQueries({
          predicate: (query) => query.queryKey[0] !== "account-session",
        })
      }
      previous.current = identity
      setSessionTransport(value, audience)
      return value
    },
    staleTime: 5000,
    retry: false,
  })
  const userId = session.data?.user?.id ?? null
  const refetchSession = session.refetch
  useEffect(() => {
    const changed = () => {
      cache.clear()
      setAccessGeneration((value) => value + 1)
      void refetchSession()
    }
    window.addEventListener("recordings-access-changed", changed)
    return () =>
      window.removeEventListener("recordings-access-changed", changed)
  }, [cache, refetchSession])
  const fragment = new URLSearchParams(location.hash.slice(1))
  const invitation = fragment.get("invite")
  const reset = fragment.get("reset")
  if (preview) return children
  if (session.isPending) return <main className="account-screen">Loading…</main>
  if (session.isError)
    return (
      <main className="account-screen">
        <p role="alert">Sign-in service unavailable.</p>
        <Button onClick={() => session.refetch()}>Try again</Button>
      </main>
    )
  const value = session.data!
  if (!value.enabled)
    return audience === "admin" ? (
      <main className="account-screen">
        Enable account mode to manage this instance.
      </main>
    ) : (
      children
    )
  if (audience === "admin" && value.user && (!value.mfa_verified || enrolling))
    return (
      <main className="account-screen">
        <AdminSecurity
          locked
          onEnrollment={() => setEnrolling(true)}
          onVerified={async () => {
            setEnrolling(false)
            await session.refetch()
          }}
        />
      </main>
    )
  if (value.user && !invitation && !reset)
    return (
      <Session.Provider key={`${userId}:${accessGeneration}`} value={value}>
        {children}
      </Session.Provider>
    )
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setPending(true)
    setError("")
    const form = new FormData(event.currentTarget)
    try {
      const password = String(form.get("password"))
      if (reset)
        await request("/api/auth/reset-password", {
          method: "POST",
          body: JSON.stringify({ token: reset, password }),
        })
      else if (invitation)
        await request("/api/auth/accept-invitation", {
          method: "POST",
          body: JSON.stringify({
            token: invitation,
            name: form.get("name"),
            password,
          }),
        })
      else
        await request("/api/auth/login", {
          method: "POST",
          body: JSON.stringify({
            email: form.get("email"),
            password,
            audience,
          }),
        })
      history.replaceState(null, "", location.pathname)
      cache.removeQueries({
        predicate: (query) => query.queryKey[0] !== "account-session",
      })
      await session.refetch()
    } catch (error) {
      setError(message(error))
    } finally {
      setPending(false)
    }
  }
  async function singleSignOn() {
    setPending(true)
    setError("")
    try {
      const response = await request<{ url: string }>("/api/auth/oidc/start", {
        method: "POST",
        body: JSON.stringify({ audience, invitation }),
      })
      location.assign(response.url)
    } catch (error) {
      setError(message(error))
      setPending(false)
    }
  }
  return (
    <main className="account-screen">
      <form onSubmit={submit} className="account-form">
        <h1>
          {reset
            ? "Reset password"
            : invitation
              ? "Accept invitation"
              : audience === "admin"
                ? "Instance administration"
                : "Sign in"}
        </h1>
        {invitation && (
          <label>
            Name
            <Input name="name" autoComplete="name" required maxLength={100} />
          </label>
        )}
        {!invitation && !reset && (
          <label>
            Email
            <Input name="email" type="email" autoComplete="username" required />
          </label>
        )}
        <label>
          Password
          <Input
            name="password"
            type="password"
            autoComplete={
              invitation || reset ? "new-password" : "current-password"
            }
            required
            minLength={invitation || reset ? 15 : undefined}
            maxLength={128}
          />
        </label>
        {(invitation || reset) && <small>Use 15–128 characters.</small>}
        <Button type="submit" disabled={pending}>
          {pending
            ? "Please wait…"
            : reset
              ? "Save password"
              : invitation
                ? "Create account"
                : "Sign in"}
        </Button>
        {value.oidc && !reset && (
          <Button
            type="button"
            variant="outline"
            onClick={singleSignOn}
            disabled={pending}
          >
            Continue with single sign-on
          </Button>
        )}
        {error && <p role="alert">{error}</p>}
        {!invitation && !reset && (
          <small>
            Ask your instance administrator for an invitation or password reset.
          </small>
        )}
      </form>
    </main>
  )
}

export function AccountControls() {
  const session = useContext(Session)
  const [open, setOpen] = useState(false)
  const [error, setError] = useState("")
  if (!session?.user) return null
  async function logout(all = false) {
    try {
      await request(all ? "/api/auth/logout-all" : "/api/auth/logout", {
        method: "POST",
      })
      location.reload()
    } catch (error) {
      setError(message(error))
    }
  }
  return (
    <div className="account-controls">
      <span>{session.user.name}</span>
      <Button variant="ghost" onClick={() => setOpen(true)}>
        Account
      </Button>
      {!!session.user.admin && <a href="/admin/">Administration</a>}
      <Button variant="ghost" onClick={() => logout()}>
        Sign out
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Account</DialogTitle>
          </DialogHeader>
          <p>{session.user.email}</p>
          <form
            className="account-form"
            onSubmit={async (event) => {
              event.preventDefault()
              const form = new FormData(event.currentTarget)
              try {
                await request("/api/auth/password", {
                  method: "POST",
                  body: JSON.stringify({
                    current: form.get("current"),
                    password: form.get("password"),
                  }),
                })
                location.reload()
              } catch (error) {
                setError(message(error))
              }
            }}
          >
            <label>
              Current password
              <Input
                name="current"
                type="password"
                autoComplete="current-password"
                required
              />
            </label>
            <label>
              New password
              <Input
                name="password"
                type="password"
                autoComplete="new-password"
                required
                minLength={15}
                maxLength={128}
              />
            </label>
            <Button type="submit">Change password and sign out</Button>
          </form>
          {session.oidc && (
            <Button
              variant="outline"
              onClick={async () => {
                try {
                  const result = await request<{ url: string }>(
                    "/api/auth/oidc/start",
                    { method: "POST", body: JSON.stringify({ link: true }) }
                  )
                  location.assign(result.url)
                } catch (error) {
                  setError(message(error))
                }
              }}
            >
              Link single sign-on
            </Button>
          )}
          <Button variant="outline" onClick={() => logout(true)}>
            Sign out all devices
          </Button>
          {error && <p role="alert">{error}</p>}
        </DialogContent>
      </Dialog>
    </div>
  )
}

type User = {
  id: string
  email: string
  name: string
  admin: number
  disabled: number
}
type Settings = {
  registration: "invite" | "closed"
  oidc: { issuer: string } | null
}
export function AdminPanel() {
  const session = useContext(Session)
  const cache = useQueryClient()
  const [error, setError] = useState("")
  const [link, setLink] = useState("")
  const users = useQuery({
    queryKey: ["admin-users"],
    queryFn: () => request<User[]>("/api/admin/users"),
  })
  const settings = useQuery({
    queryKey: ["admin-settings"],
    queryFn: () => request<Settings>("/api/admin/settings"),
  })
  const invitations = useQuery({
    queryKey: ["admin-invitations"],
    queryFn: () =>
      request<{ email: string; expires: number; used: number | null }[]>(
        "/api/admin/invitations"
      ),
  })
  const audit = useQuery({
    queryKey: ["admin-audit"],
    queryFn: () =>
      request<
        { id: number; action: string; target: string | null; created: number }[]
      >("/api/admin/audit"),
  })
  async function act(path: string, method: string, body?: unknown) {
    setError("")
    setLink("")
    try {
      const result = await request<{ token?: string }>(path, {
        method,
        body: body ? JSON.stringify(body) : undefined,
      })
      if (result.token)
        setLink(
          `${location.origin}/#${path.endsWith("/reset") ? "reset" : "invite"}=${result.token}`
        )
      await cache.invalidateQueries({
        predicate: (q) => String(q.queryKey[0]).startsWith("admin-"),
      })
    } catch (error) {
      setError(message(error))
    }
  }
  return (
    <main className="admin-screen">
      <header>
        <h1>Instance administration</h1>
        <a href="/">Recordings</a>
        <Button
          variant="outline"
          onClick={async () => {
            await request("/api/auth/logout", { method: "POST" })
            location.reload()
          }}
        >
          Sign out
        </Button>
      </header>
      <p>{session?.user?.email}</p>
      <details>
        <summary className="admin-security-toggle">
          Passkeys and recovery
        </summary>
        <AdminSecurity
          onVerified={async () => {
            await cache.refetchQueries({
              queryKey: ["account-session", "admin"],
            })
          }}
        />
      </details>
      {(error || users.isError || settings.isError) && (
        <p role="alert">
          {error ||
            "Administration could not be loaded. Sign in again and retry."}
        </p>
      )}
      {link && (
        <section className="admin-card">
          <label>
            Share this one-time link
            <Input
              aria-label="One-time link"
              value={link}
              readOnly
              onFocus={(event) => event.target.select()}
            />
          </label>
          <Button variant="outline" onClick={() => setLink("")}>
            Dismiss
          </Button>
        </section>
      )}
      <section className="admin-card">
        <h2>Registration</h2>
        {settings.data && (
          <ValueSelect
            label="Registration policy"
            value={settings.data.registration}
            onChange={(registration) =>
              act("/api/admin/settings", "PATCH", { registration })
            }
            items={[
              { value: "invite", label: "Invitation only" },
              { value: "closed", label: "Closed" },
            ]}
          />
        )}
        <p>
          {settings.data?.oidc
            ? `Single sign-on: ${settings.data.oidc.issuer}`
            : "Single sign-on is not configured."}
        </p>
      </section>
      <section className="admin-card">
        <h2>Invite a user</h2>
        <form
          className="admin-inline"
          onSubmit={(event) => {
            event.preventDefault()
            const form = new FormData(event.currentTarget)
            void act("/api/admin/invitations", "POST", {
              email: form.get("email"),
            })
          }}
        >
          <Input
            aria-label="Invitation email"
            name="email"
            type="email"
            placeholder="Email address"
            required
          />
          <Button
            type="submit"
            disabled={settings.data?.registration !== "invite"}
          >
            Create invitation
          </Button>
        </form>
        {invitations.data
          ?.filter((invite) => !invite.used)
          .map((invite) => (
            <div className="admin-row" key={invite.email}>
              <span>{invite.email}</span>
              <Button
                variant="ghost"
                onClick={() =>
                  act("/api/admin/invitations", "DELETE", {
                    email: invite.email,
                  })
                }
              >
                Revoke invitation
              </Button>
            </div>
          ))}
      </section>
      <section className="admin-card">
        <h2>Users</h2>
        {users.data?.map((user) => (
          <article className="admin-row" key={user.id}>
            <div>
              <strong>{user.name}</strong>
              <p>{user.email}</p>
              <small>
                {user.disabled
                  ? "Suspended"
                  : user.admin
                    ? "Administrator"
                    : "Member"}
              </small>
            </div>
            <div className="admin-actions">
              <Button
                variant="outline"
                onClick={() =>
                  act(`/api/admin/users/${user.id}`, "PATCH", {
                    admin: !user.admin,
                    disabled: !!user.disabled,
                  })
                }
              >
                {user.admin ? "Remove admin" : "Make admin"}
              </Button>
              <Button
                variant="outline"
                onClick={() =>
                  act(`/api/admin/users/${user.id}`, "PATCH", {
                    admin: !!user.admin,
                    disabled: !user.disabled,
                  })
                }
              >
                {user.disabled ? "Reactivate" : "Suspend"}
              </Button>
              <Button
                variant="outline"
                onClick={() => act(`/api/admin/users/${user.id}/reset`, "POST")}
              >
                Password reset
              </Button>
            </div>
          </article>
        ))}
      </section>
      <details className="admin-card">
        <summary>Recent account activity</summary>
        {audit.data?.map((row) => (
          <p key={row.id}>
            {new Date(row.created * 1000).toLocaleString()} · {row.action}{" "}
            {row.target}
          </p>
        ))}
      </details>
    </main>
  )
}

export function ProjectMembers({ projectId }: { projectId: string }) {
  const cache = useQueryClient()
  const [open, setOpen] = useState(false)
  const [error, setError] = useState("")
  const [role, setRole] = useState("viewer")
  const members = useQuery({
    queryKey: ["members", projectId],
    enabled: open,
    queryFn: () =>
      request<{ email: string; name: string; role: string }[]>(
        `/api/projects/${projectId}/members`
      ),
  })
  async function update(email: string, remove = false) {
    try {
      await request(`/api/projects/${projectId}/members`, {
        method: remove ? "DELETE" : "PUT",
        body: JSON.stringify(remove ? { email } : { email, role }),
      })
      await cache.invalidateQueries({ queryKey: ["members", projectId] })
      setError("")
    } catch (error) {
      setError(message(error))
    }
  }
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)}>
        Project members
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Project members</DialogTitle>
          </DialogHeader>
          <form
            className="account-form"
            onSubmit={(event) => {
              event.preventDefault()
              void update(
                String(new FormData(event.currentTarget).get("email"))
              )
            }}
          >
            <label>
              Account email
              <Input name="email" type="email" required />
            </label>
            <ValueSelect
              label="Project role"
              value={role}
              onChange={setRole}
              items={[
                { value: "viewer", label: "Viewer" },
                { value: "editor", label: "Editor" },
                { value: "owner", label: "Owner" },
              ]}
            />
            <Button type="submit">Add or update member</Button>
          </form>
          {members.data?.map((member) => (
            <div className="admin-row" key={member.email}>
              <span>
                {member.email} · {member.role}
              </span>
              <Button
                variant="ghost"
                onClick={() => update(member.email, true)}
              >
                Remove
              </Button>
            </div>
          ))}
          {error && <p role="alert">{error}</p>}
        </DialogContent>
      </Dialog>
    </>
  )
}
