import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { request } from "@/lib/api"
import type { components } from "@/lib/schema"

type State = components["schemas"]["AIAdminState"]
type Controls = components["schemas"]["AIControls"]
const fields = [
  ["report_soft_usd", "Per report soft limit"],
  ["report_hard_usd", "Per report hard limit"],
  ["monthly_soft_usd", "Monthly soft limit"],
  ["monthly_hard_usd", "Monthly hard limit"],
] as const

function ControlsForm({
  state,
  save,
  busy,
}: {
  state: State
  save: (value: Controls) => void
  busy: boolean
}) {
  const [enabled, setEnabled] = useState(state.controls.enabled)
  const [amounts, setAmounts] = useState(
    Object.fromEntries(
      fields.map(([key]) => [key, state.controls[key]?.toString() ?? ""])
    )
  )
  const [error, setError] = useState("")
  return (
    <form
      className="admin-ai-form"
      onSubmit={(event) => {
        event.preventDefault()
        const value = { enabled } as Controls
        for (const [key] of fields) {
          const text = amounts[key].trim()
          value[key] = text ? Number(text) : null
          if (text && (!Number.isFinite(value[key]) || value[key]! <= 0)) {
            setError(
              "Enter positive USD amounts, or leave a field blank for no limit."
            )
            return
          }
        }
        for (const prefix of ["report", "monthly"] as const) {
          const soft = value[`${prefix}_soft_usd`],
            hard = value[`${prefix}_hard_usd`]
          if (soft != null && hard != null && soft > hard) {
            setError("A soft limit cannot exceed its hard limit.")
            return
          }
        }
        setError("")
        save(value)
      }}
    >
      <label className="admin-ai-enabled">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
          disabled={busy}
        />{" "}
        Enable report generation
      </label>
      <p>
        Soft limits show an alert and ask generation to finish concisely,
        without further visual research. Hard limits block the next paid call
        before its conservative cost estimate would exceed the limit.
      </p>
      <div className="admin-ai-limits">
        {fields.map(([key, label]) => (
          <label key={key}>
            {label} (USD)
            <Input
              type="number"
              min="0.000001"
              max="1000000"
              step="any"
              value={amounts[key]}
              placeholder="No limit"
              disabled={busy}
              onChange={(event) =>
                setAmounts({ ...amounts, [key]: event.target.value })
              }
            />
          </label>
        ))}
      </div>
      <p className="muted">
        Blank means no limit. Monthly limits apply to this instance and reset on
        the first day of each UTC month. Estimated usage includes uncertain
        calls; this is separate from OpenAI billing and provider limits. A hard
        limit can stop a report before a draft is saved.
      </p>
      {error && <p role="alert">{error}</p>}
      <Button type="submit" disabled={busy}>
        {busy ? "Saving…" : "Save AI limits"}
      </Button>
    </form>
  )
}

export function AdminAI() {
  const cache = useQueryClient()
  const state = useQuery({
    queryKey: ["admin-ai"],
    queryFn: () => request<State>("/api/admin/ai"),
  })
  const [key, setKey] = useState("")
  const [message, setMessage] = useState("")
  const save = useMutation({
    mutationFn: ({
      path,
      method,
      body,
    }: {
      path: string
      method: string
      body?: unknown
    }) =>
      request<State>(path, {
        method,
        body: body === undefined ? undefined : JSON.stringify(body),
      }),
    onMutate: () => setMessage(""),
    onSuccess: (value, variables) => {
      cache.setQueryData(["admin-ai"], value)
      setKey("")
      setMessage(
        variables.path.endsWith("/key")
          ? "API key configuration saved. The key is never displayed."
          : "AI limits saved. They apply before the next paid call."
      )
      void cache.invalidateQueries({ queryKey: ["admin-audit"] })
    },
    onError: () => setKey(""),
    onSettled: (_data, _error, variables) => {
      const body = variables.body
      if (body && typeof body === "object" && "api_key" in body)
        body.api_key = ""
    },
  })
  return (
    <section className="admin-card" aria-label="AI reports configuration">
      <h2>AI reports</h2>
      {state.isPending ? (
        <p>Loading AI settings…</p>
      ) : state.error ? (
        <p role="alert">Could not load AI settings: {state.error.message}</p>
      ) : (
        state.data && (
          <>
            <p>
              Estimated usage this month: $
              {state.data.month_cost_usd.toFixed(4)}
            </p>
            <ControlsForm
              key={JSON.stringify(state.data.controls)}
              state={state.data}
              busy={save.isPending}
              save={(body) =>
                save.mutate({ path: "/api/admin/ai", method: "PUT", body })
              }
            />
            <details>
              <summary>OpenAI API key</summary>
              <p>
                {state.data.key_configured
                  ? `A key is configured through ${state.data.key_source === "deployment" ? "the deployment" : "administration"}.`
                  : "No API key is configured."}
              </p>
              <p className="muted">
                An administrator key takes precedence over the deployment key.
                It is stored as a private file in the coordinator data volume
                and included in its private backups. It is never returned to
                browsers or written to the audit log. Saving does not make a
                paid test request.
              </p>
              <form
                className="admin-ai-form"
                onSubmit={(event) => {
                  event.preventDefault()
                  save.mutate({
                    path: "/api/admin/ai/key",
                    method: "PUT",
                    body: { api_key: key },
                  })
                }}
              >
                <label>
                  New OpenAI API key
                  <Input
                    type="password"
                    autoComplete="off"
                    minLength={20}
                    maxLength={4096}
                    value={key}
                    onChange={(event) => setKey(event.target.value)}
                    disabled={save.isPending}
                    required
                  />
                </label>
                <Button type="submit" disabled={save.isPending || !key.trim()}>
                  {save.isPending ? "Saving…" : "Save API key"}
                </Button>
              </form>
              {state.data.key_source === "administration" && (
                <Button
                  variant="outline"
                  disabled={save.isPending}
                  onClick={() =>
                    save.mutate({ path: "/api/admin/ai/key", method: "DELETE" })
                  }
                >
                  Remove administrator key
                </Button>
              )}
            </details>
          </>
        )
      )}
      {save.error && (
        <p role="alert">Could not save AI settings: {save.error.message}</p>
      )}
      {message && <p role="status">{message}</p>}
    </section>
  )
}
