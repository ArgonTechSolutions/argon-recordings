import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { request } from "@/lib/api"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import type { components } from "@/lib/schema"

type SystemState = components["schemas"]["SystemState"]
export function AdminSystem() {
  const cache = useQueryClient()
  const [target, setTarget] = useState<string | null>(null)
  const state = useQuery({
    queryKey: ["admin-system"],
    queryFn: () => request<SystemState>("/api/admin/system"),
    refetchInterval: 5000,
  })
  const update = useMutation({
    mutationFn: (target: string) =>
      request("/api/admin/system/updates", {
        method: "POST",
        body: JSON.stringify({ target, confirm: "update-latest-main" }),
      }),
    onSuccess: () => {
      setTarget(null)
      void cache.invalidateQueries({ queryKey: ["admin-system"] })
      void cache.invalidateQueries({ queryKey: ["admin-audit"] })
    },
  })
  const cancel = useMutation({
    mutationFn: (id: string) =>
      request(`/api/admin/system/updates/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["admin-system"] })
      void cache.invalidateQueries({ queryKey: ["admin-audit"] })
    },
  })
  const active = state.data?.updates.some(
    (update) => update.state === "queued" || update.state === "running"
  )
  return (
    <>
      <section className="admin-card">
        <h2>Worker status</h2>
        <p>
          Refreshes every five seconds. Workers are offline after 130 seconds
          without authenticated contact.
        </p>
        {state.isPending && <p>Loading system status…</p>}
        {state.error && (
          <p role="alert">
            System status unavailable. The app may be restarting; this page will
            retry: {state.error.message}
          </p>
        )}
        {state.data?.workers.length === 0 && <p>No workers configured.</p>}
        {state.data?.workers.map((worker) => (
          <article className="admin-row" key={worker.id}>
            <div>
              <h3>{worker.id}</h3>
              <p>
                {worker.online ? "Online" : "Offline"} ·{" "}
                {worker.last_contact_seconds == null
                  ? "No contact since app startup"
                  : `Last contact ${Math.floor(worker.last_contact_seconds)}s ago`}
              </p>
              <p>
                {worker.active_tasks} active task leases · Speaker
                identification{" "}
                {worker.diarization_available && worker.online
                  ? "available"
                  : "unavailable"}
              </p>
            </div>
          </article>
        ))}
      </section>
      <section className="admin-card">
        <h2>Application & worker updates</h2>
        <p>
          Update a host to the latest version on the main release branch. The
          main app and any workers on the same host update together. Remote
          worker hosts update separately.
        </p>
        <p>
          Updates require the host update runner to be enabled by the
          installation owner. Updates create verified backups and retain them on
          failure; restoring a backup requires host recovery. Services on the
          selected host stop during the update; wait for active work to finish
          first.
        </p>
        {!state.data?.hosts.length && (
          <p>
            No host runners connected. Enable the admin-runner command on each
            installation host to use these controls.
          </p>
        )}
        {state.data?.hosts.map((host) => (
          <article className="admin-row" key={host.target}>
            <div>
              <h3>
                {host.target === "app"
                  ? "Main app & local workers"
                  : `Remote ${host.target}`}
              </h3>
              <p>Update runner: {host.online ? "Connected" : "Offline"}</p>
              <small>Host source revision: {host.source_revision}</small>
            </div>
            <Button
              disabled={!host.online || active || update.isPending}
              onClick={() => {
                update.reset()
                setTarget(host.target)
              }}
            >
              Update host
            </Button>
          </article>
        ))}
        <h3>Recent updates</h3>
        {state.data?.updates.length === 0 && <p>No updates requested.</p>}
        {state.data?.updates.map((item) => (
          <article className="admin-row" key={item.id}>
            <div>
              <strong>
                {item.target} · {item.state}
              </strong>
              <p>{new Date(item.updated * 1000).toLocaleString()}</p>
              {item.error && <p role="alert">{item.error}</p>}
              {item.state === "running" && (
                <p>
                  If the host runner is offline, inspect the host before
                  retrying. Interrupted updates are never replayed
                  automatically.
                </p>
              )}
            </div>
            {item.state === "queued" && (
              <Button
                variant="outline"
                disabled={cancel.isPending}
                onClick={() => cancel.mutate(item.id)}
              >
                Cancel update
              </Button>
            )}
          </article>
        ))}
        {cancel.error && <p role="alert">{cancel.error.message}</p>}
      </section>
      <Dialog
        open={target !== null}
        onOpenChange={(open) => {
          if (!open && !update.isPending) setTarget(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Update {target === "app" ? "main app & local workers" : target}?
            </DialogTitle>
          </DialogHeader>
          <p>
            This stops services on this host and updates to the latest main
            branch using the existing backup and rollback procedure. Finish
            active work before continuing. The main app may be unavailable
            briefly.
          </p>
          <Button
            disabled={update.isPending}
            onClick={() => target && update.mutate(target)}
          >
            {update.isPending ? "Requesting…" : "Confirm update"}
          </Button>
          <Button
            variant="outline"
            disabled={update.isPending}
            onClick={() => setTarget(null)}
          >
            Cancel
          </Button>
          {update.error && <p role="alert">{update.error.message}</p>}
        </DialogContent>
      </Dialog>
    </>
  )
}
