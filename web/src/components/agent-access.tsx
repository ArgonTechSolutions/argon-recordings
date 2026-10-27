import { useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ValueSelect } from "@/components/value-select"
import { request, type Project } from "@/lib/api"

import type { components } from "@/lib/schema"

type Grant = components["schemas"]["GrantView"]

export function AgentAccess() {
  const cache = useQueryClient()
  const [open, setOpen] = useState(false),
    [projectId, setProjectId] = useState("")
  const [label, setLabel] = useState(""),
    [derive, setDerive] = useState(false),
    [days, setDays] = useState("7")
  const [token, setToken] = useState(""),
    [pending, setPending] = useState(false),
    [error, setError] = useState("")
  const grants = useQuery({
    queryKey: ["agent-grants"],
    enabled: open,
    queryFn: () => request<Grant[]>("/api/auth/agent-grants"),
  })
  const projects = useQuery({
    queryKey: ["projects"],
    enabled: open,
    queryFn: () => request<Project[]>("/api/projects"),
  })
  const selected = projects.data?.find((p) => p.id === projectId)
  const change = async (revokeId?: string) => {
    setPending(true)
    setError("")
    setToken("")
    try {
      if (revokeId)
        await request(`/api/auth/agent-grants/${revokeId}`, {
          method: "DELETE",
        })
      else {
        const issued = await request<{ token: string }>(
          "/api/auth/agent-grants",
          {
            method: "POST",
            body: JSON.stringify({
              label,
              project_ids: [projectId],
              derive_media: derive,
              expires_in_days: Number(days),
            }),
          }
        )
        // Deliberately held only in this mounted view; never Query cache or storage.
        setToken(issued.token)
      }
      await cache.invalidateQueries({ queryKey: ["agent-grants"] })
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Agent access unavailable"
      )
    } finally {
      setPending(false)
    }
  }
  return (
    <details
      className="agent-access"
      onToggle={(e) => {
        const next = e.currentTarget.open
        setOpen(next)
        if (!next) setToken("")
      }}
    >
      <summary>Agent access</summary>
      <p>Connect your agent to read recordings and create cited summaries.</p>
      <details>
        <summary>Connection setup</summary>
        <p>
          Use the Argon Recordings MCP package and recording-summary skill in
          your agent client. Create access below, then save the token in a
          private credential file. Enable screenshot requests if the agent needs
          new images.
        </p>
        <label>
          API origin
          <Input
            aria-label="MCP API origin"
            value={window.location.origin}
            readOnly
            onFocus={(e) => e.currentTarget.select()}
          />
        </label>
        <p>Import the generated report draft under Reports in the recording.</p>
      </details>
      <form
        className="account-form"
        onSubmit={(e) => {
          e.preventDefault()
          void change()
        }}
      >
        <label>
          Name
          <Input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            maxLength={80}
            required
            aria-label="Agent name"
          />
        </label>
        <ValueSelect
          label="Agent project"
          value={projectId || "none"}
          onChange={(value) => {
            setProjectId(value === "none" ? "" : value)
            setDerive(false)
          }}
          items={[
            { value: "none", label: "Choose project" },
            ...(projects.data?.map((p) => ({ value: p.id, label: p.name })) ??
              []),
          ]}
        />
        <ValueSelect
          label="Agent access expiry"
          value={days}
          onChange={setDays}
          items={[
            { value: "1", label: "1 day" },
            { value: "7", label: "7 days" },
            { value: "30", label: "30 days" },
          ]}
        />
        <label>
          <input
            type="checkbox"
            checked={derive}
            disabled={!selected || selected.role === "viewer"}
            onChange={(e) => setDerive(e.target.checked)}
          />{" "}
          Allow screenshot and clip requests
        </label>
        <Button type="submit" disabled={pending || !projectId}>
          Create access
        </Button>
      </form>
      {token && (
        <div className="account-form">
          <p>Shown once. Save in your client's private credential store.</p>
          <Input
            type="password"
            readOnly
            value={token}
            aria-label="New agent token"
            autoComplete="off"
            onFocus={(e) => e.currentTarget.select()}
          />
          <Button variant="outline" onClick={() => setToken("")}>
            Hide token
          </Button>
        </div>
      )}
      {error && <p role="alert">{error}</p>}
      {grants.isError && <p role="alert">{grants.error.message}</p>}
      <ul>
        {grants.data?.map((grant) => (
          <li key={grant.id}>
            <span>
              {grant.label} ·{" "}
              {grant.revoked
                ? "Revoked"
                : grant.expired
                  ? "Expired"
                  : new Date(grant.expires * 1000).toLocaleDateString()}
            </span>
            {!grant.revoked && !grant.expired && (
              <Button
                size="sm"
                variant="ghost"
                disabled={pending}
                onClick={() => void change(grant.id)}
              >
                Revoke
              </Button>
            )}
          </li>
        ))}
      </ul>
    </details>
  )
}
