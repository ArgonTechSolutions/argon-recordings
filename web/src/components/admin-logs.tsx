import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { request } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { ValueSelect } from "@/components/value-select"
import type { components } from "@/lib/schema"

type LogPage = components["schemas"]["LogPage"]
export function AdminLogs() {
  const [category, setCategory] = useState("all")
  const [source, setSource] = useState("all")
  const [before, setBefore] = useState<number | null>(null)
  const logs = useQuery({
    queryKey: ["admin-logs", category, source, before],
    queryFn: () => {
      const params = new URLSearchParams()
      if (category !== "all") params.set("category", category)
      if (source !== "all") params.set("source", source)
      if (before != null) params.set("before", String(before))
      return request<LogPage>(`/api/admin/logs?${params}`)
    },
    refetchInterval: before == null ? 10000 : false,
  })
  return (
    <section className="admin-card">
      <h2>Operational logs</h2>
      <p>
        Errors and summaries from the app and workers. Repeated identical events
        within a minute are grouped. The latest 10,000 entries are retained;
        credentials and recording content are excluded.
      </p>
      <div className="admin-inline">
        <ValueSelect
          label="Log category"
          value={category}
          onChange={(value) => {
            setCategory(value)
            setBefore(null)
          }}
          items={[
            { value: "all", label: "All categories" },
            { value: "errors", label: "Errors" },
            { value: "usage", label: "Usage summaries" },
            { value: "updates", label: "Updates" },
            { value: "system", label: "System" },
          ]}
        />
        <ValueSelect
          label="Log source"
          value={source}
          onChange={(value) => {
            setSource(value)
            setBefore(null)
          }}
          items={[
            { value: "all", label: "App & all workers" },
            { value: "app", label: "Main app" },
            { value: "legacy", label: "Legacy worker" },
            ...Array.from({ length: 8 }, (_, i) => ({
              value: `worker-${i + 1}`,
              label: `Worker ${i + 1}`,
            })),
          ]}
        />
        <Button
          variant="outline"
          onClick={() => {
            setBefore(null)
            void logs.refetch()
          }}
        >
          Latest logs
        </Button>
      </div>
      {logs.isPending && <p>Loading logs…</p>}
      {logs.error && (
        <p role="alert">Could not load logs: {logs.error.message}</p>
      )}
      {logs.data?.entries.length === 0 && (
        <p>No events in this category yet.</p>
      )}
      <ol className="operational-logs">
        {logs.data?.entries.map((entry) => (
          <li key={entry.id} className={`log-entry log-${entry.level}`}>
            <div>
              <strong>
                {entry.source} · {entry.category}
              </strong>
              <span>
                {entry.level}
                {entry.occurrences > 1
                  ? ` · ${entry.occurrences} occurrences`
                  : ""}
              </span>
            </div>
            <time dateTime={new Date(entry.created * 1000).toISOString()}>
              {new Date(entry.created * 1000).toLocaleString()}
            </time>
            <p>{entry.summary}</p>
          </li>
        ))}
      </ol>
      {logs.data?.next_before != null && (
        <Button
          variant="outline"
          onClick={() => setBefore(logs.data!.next_before!)}
        >
          Older logs
        </Button>
      )}
    </section>
  )
}
