import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { preview, request, type Recording } from "@/lib/api"
import type { components } from "@/lib/schema"

type Job = components["schemas"]["VisualJob"]
type Detail = components["schemas"]["AnalysisDetail"]
type Evidence = components["schemas"]["EvidenceSnapshot"]
const stamp = (t: number) =>
  `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, "0")}`

export function VisualAnalysis({
  recording,
  canEdit,
  seek,
}: {
  recording: Recording
  canEdit: boolean
  seek: (time: number) => void
}) {
  const cache = useQueryClient()
  const base = `/api/recordings/${recording.id}`
  const [start, setStart] = useState(0)
  const [selectedEnd, setEnd] = useState<number | null>(null)
  const end = selectedEnd ?? Math.min(recording.duration ?? 0, 300)
  const [selected, setSelected] = useState<string | null>(null)
  const enabled =
    !preview && !!recording.artifacts?.some((a) => a.kind === "frame")
  const jobs = useQuery({
    queryKey: ["analysis-jobs", recording.id],
    queryFn: () => request<Job[]>(base + "/analysis-jobs"),
    enabled,
  })
  const jid = selected ?? jobs.data?.[0]?.id
  const detail = useQuery({
    queryKey: ["analysis", recording.id, jid],
    queryFn: () => request<Detail>(`${base}/analysis-jobs/${jid}`),
    enabled: enabled && !!jid,
  })
  const mutation = useMutation({
    mutationFn: async (
      action:
        | { kind: "analyze" }
        | { kind: "cancel" }
        | { kind: "accept" | "reject"; ordinal: number }
    ) => {
      if (action.kind === "cancel")
        return request<Job>(`${base}/analysis-jobs/${jid}/cancel`, {
          method: "POST",
        })
      const state = await request<Evidence>(base + "/evidence")
      if (action.kind === "analyze")
        return request<Job>(base + "/analysis-jobs", {
          method: "POST",
          body: JSON.stringify({
            reference: {
              recording_id: recording.id,
              source_sha256: state.source_sha256,
              versions: state.versions,
              interval: { start, end },
            },
          }),
        })
      return request<Detail>(
        `${base}/analysis-jobs/${jid}/proposals/${action.ordinal}`,
        {
          method: "POST",
          body: JSON.stringify({
            choice: action.kind,
            expected_revision: state.versions.annotations,
            expected_source_sha256: state.source_sha256,
          }),
        }
      )
    },
    onSuccess: (job) => {
      setSelected(job.id)
      for (const kind of ["analysis-jobs", "analysis", "evidence", "marks"])
        void cache.invalidateQueries({ queryKey: [kind, recording.id] })
    },
  })
  if (!enabled) return null
  const data = detail.data
  const busy = data?.state === "queued" || data?.state === "running"
  return (
    <details className="visual-analysis">
      <summary>Visual suggestions</summary>
      {canEdit && (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            mutation.mutate({ kind: "analyze" })
          }}
        >
          <div className="evidence-range">
            <label>
              Start
              <Input
                aria-label="Analysis start"
                type="number"
                min={0}
                max={recording.duration ?? 0}
                step="0.1"
                value={start}
                onChange={(event) => setStart(Number(event.target.value))}
              />
            </label>
            <label>
              End
              <Input
                aria-label="Analysis end"
                type="number"
                min={start}
                max={recording.duration ?? 0}
                step="0.1"
                value={end}
                onChange={(event) => setEnd(Number(event.target.value))}
              />
            </label>
          </div>
          <p className="muted">
            Scans up to 5 minutes. Keep suggestions as marks, then extract
            evidence.
          </p>
          <Button
            type="submit"
            size="sm"
            disabled={mutation.isPending || busy || end <= start}
          >
            Analyze visuals
          </Button>
        </form>
      )}
      {busy && (
        <div role="status">
          <span>{data.state === "queued" ? "Queued" : "Analyzing"}</span>
          {canEdit && (
            <Button
              size="sm"
              variant="ghost"
              disabled={mutation.isPending}
              onClick={() => mutation.mutate({ kind: "cancel" })}
            >
              Cancel analysis
            </Button>
          )}
        </div>
      )}
      {data?.error && <p role="alert">{data.error}</p>}
      {data?.result && (
        <>
          <p className="muted">
            Scanned {stamp(data.result.scanned_interval.start)}–
            {stamp(data.result.scanned_interval.end)}
            {data.result.status === "partial"
              ? ` · Partial: ${data.result.partial_reason}`
              : ""}
          </p>
          <p className="muted">
            Heuristic scores indicate visual change, not confidence in meaning.
          </p>
          <ul className="proposal-list">
            {data.proposals.map((p) => (
              <li key={p.ordinal}>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => seek(p.timestamp)}
                >
                  {p.type === "image" ? "Still" : "Motion"}{" "}
                  {stamp(p.source_start)}
                  {p.type === "clip" ? `–${stamp(p.source_end)}` : ""}
                </Button>
                <p>
                  {p.reason} · {Math.round(p.score * 100)}
                  {p.fallback ? " · Sparse reference" : ""}
                </p>
                {p.choice ? (
                  <span className="muted">
                    {p.choice === "reject"
                      ? "Rejected"
                      : p.mark_active
                        ? "Marked"
                        : "Mark removed"}
                  </span>
                ) : (
                  canEdit && (
                    <div className="evidence-list">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={mutation.isPending || p.manual_conflict}
                        onClick={() =>
                          mutation.mutate({
                            kind: "accept",
                            ordinal: p.ordinal,
                          })
                        }
                      >
                        Keep mark
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={mutation.isPending}
                        onClick={() =>
                          mutation.mutate({
                            kind: "reject",
                            ordinal: p.ordinal,
                          })
                        }
                      >
                        Reject
                      </Button>
                      {p.manual_conflict && (
                        <span className="muted">
                          Manual mark takes priority
                        </span>
                      )}
                    </div>
                  )
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      {(mutation.isError || jobs.isError || detail.isError) && (
        <p role="alert">
          {mutation.error?.message ??
            jobs.error?.message ??
            detail.error?.message}
        </p>
      )}
    </details>
  )
}
