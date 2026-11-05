import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { request } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import type { components } from "@/lib/schema"

type Usage = components["schemas"]["MonthlyUsage"]
type Budgets = components["schemas"]["UserBudgetState"]

export function MonthlyUsage() {
  const usage = useQuery({
    queryKey: ["account-usage"],
    queryFn: () => request<Usage>("/api/account/usage"),
    refetchInterval: 15000,
  })
  return (
    <section className="monthly-usage" aria-label="Monthly AI usage">
      <h2>Monthly AI usage</h2>
      {usage.isPending ? (
        <p>Loading usage…</p>
      ) : usage.error ? (
        <p role="alert">Could not load usage: {usage.error.message}</p>
      ) : (
        usage.data && (
          <>
            <p>
              {usage.data.period} (UTC) · ${usage.data.usage_usd.toFixed(4)}{" "}
              used
            </p>
            <p>
              Monthly cap:{" "}
              {usage.data.cap_usd == null
                ? "Unlimited"
                : `$${usage.data.cap_usd.toFixed(4)}`}{" "}
              ·{" "}
              {usage.data.cap_source === "user"
                ? "Your override"
                : "Instance default"}
            </p>
            {usage.data.cap_usd != null && (
              <p>
                $
                {Math.max(0, usage.data.cap_usd - usage.data.usage_usd).toFixed(
                  4
                )}{" "}
                remaining
              </p>
            )}
            <small>
              Estimated AI costs include pending and uncertain calls. Resets on
              the first day of each UTC month. This is separate from provider
              billing.
            </small>
          </>
        )
      )}
    </section>
  )
}

function CapForm({
  value,
  inherited,
  save,
  reset,
  busy,
}: {
  value: number | null
  inherited?: boolean
  save: (cap: number | null) => void
  reset?: () => void
  busy: boolean
}) {
  const [amount, setAmount] = useState(value?.toString() ?? "")
  return (
    <form
      className="admin-inline"
      onSubmit={(event) => {
        event.preventDefault()
        save(amount.trim() ? Number(amount) : null)
      }}
    >
      <label>
        Monthly cap (USD)
        <Input
          aria-label="Monthly cap in USD"
          type="number"
          min="0"
          max="1000000"
          step="any"
          placeholder="Unlimited"
          value={amount}
          onChange={(event) => setAmount(event.target.value)}
          disabled={busy}
        />
      </label>
      <Button type="submit" disabled={busy}>
        {inherited ? "Set override" : "Save cap"}
      </Button>
      {reset && !inherited && (
        <Button type="button" variant="outline" disabled={busy} onClick={reset}>
          Use default
        </Button>
      )}
    </form>
  )
}

export function AdminBudgets({
  users,
}: {
  users: { id: string; name: string; email: string }[]
}) {
  const cache = useQueryClient()
  const budgets = useQuery({
    queryKey: ["admin-budgets"],
    queryFn: () => request<Budgets>("/api/admin/ai/budgets"),
    refetchInterval: 15000,
  })
  const [saved, setSaved] = useState(false)
  const change = useMutation({
    mutationFn: ({
      path,
      cap,
      reset,
    }: {
      path: string
      cap?: number | null
      reset?: boolean
    }) =>
      request<Budgets>(path, {
        method: reset ? "DELETE" : "PUT",
        body: reset ? undefined : JSON.stringify({ cap_usd: cap }),
      }),
    onMutate: () => setSaved(false),
    onSuccess: (value) => {
      cache.setQueryData(["admin-budgets"], value)
      setSaved(true)
      void cache.invalidateQueries({ queryKey: ["admin-audit"] })
    },
  })
  return (
    <section className="admin-card">
      <h2>Monthly user budgets</h2>
      <p>
        The default applies to each user. A user override replaces it. Blank
        means unlimited; zero blocks paid calls. Server-wide limits still apply
        separately.
      </p>
      <p className="muted">
        Resets each UTC month. Usage includes pending and uncertain API costs.
        Caps are checked against a conservative cost estimate before every paid
        call, so a report may stop before a draft is saved.
      </p>
      {budgets.isPending ? (
        <p>Loading budgets…</p>
      ) : budgets.error ? (
        <p role="alert">Could not load budgets: {budgets.error.message}</p>
      ) : (
        budgets.data && (
          <>
            <h3>Default for all users</h3>
            <CapForm
              key={`default-${budgets.data.default_cap_usd}`}
              value={budgets.data.default_cap_usd}
              busy={change.isPending}
              save={(cap) =>
                change.mutate({ path: "/api/admin/ai/budgets/default", cap })
              }
            />
            {budgets.data.users.map((usage) => {
              const user = users.find((user) => user.id === usage.user_id)
              return (
                <article className="budget-user" key={usage.user_id}>
                  <h3>{user?.name ?? usage.user_id}</h3>
                  <p>{user?.email}</p>
                  <p>
                    {usage.period} · ${usage.usage_usd.toFixed(4)} used ·{" "}
                    {usage.cap_source === "user"
                      ? "User override"
                      : "Using default"}
                    :{" "}
                    {usage.cap_usd == null
                      ? "Unlimited"
                      : `$${usage.cap_usd.toFixed(4)}`}
                  </p>
                  <CapForm
                    key={`${usage.user_id}-${usage.cap_usd}-${usage.cap_source}`}
                    value={usage.cap_usd}
                    inherited={usage.cap_source !== "user"}
                    busy={change.isPending}
                    save={(cap) =>
                      change.mutate({
                        path: `/api/admin/ai/budgets/users/${usage.user_id}`,
                        cap,
                      })
                    }
                    reset={() =>
                      change.mutate({
                        path: `/api/admin/ai/budgets/users/${usage.user_id}`,
                        reset: true,
                      })
                    }
                  />
                </article>
              )
            })}
          </>
        )
      )}
      {change.error && (
        <p role="alert">Could not save cap: {change.error.message}</p>
      )}
      {saved && (
        <p role="status">Cap saved. It applies before the next paid call.</p>
      )}
    </section>
  )
}
