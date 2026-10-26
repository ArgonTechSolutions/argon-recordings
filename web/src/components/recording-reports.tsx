import { useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { request, type Recording } from "@/lib/api"
import type { components } from "@/lib/schema"

type Reports = components["schemas"]["ReportsList"]
type Detail = components["schemas"]["ReportDetail"]

export function RecordingReports({
  recording,
  canEdit,
  seek,
}: {
  recording: Recording
  canEdit: boolean
  seek: (time: number) => void
}) {
  const cache = useQueryClient()
  const base = `/api/recordings/${recording.id}/reports`
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null)
  const [filename, setFilename] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const reports = useQuery({
    queryKey: ["reports", recording.id],
    queryFn: () => request<Reports>(base),
    enabled: !!recording.sha256 && recording.duration !== null,
  })
  const detail = useQuery({
    queryKey: ["report", recording.id, selected],
    queryFn: () => request<Detail>(`${base}/${selected}`),
    enabled: !!selected,
  })
  const mutation = useMutation({
    mutationFn: async (review: boolean) => {
      const current = await request<Reports>(base)
      return request<components["schemas"]["ReportView"]>(
        review ? `${base}/${selected}/review` : base,
        {
          method: "POST",
          body: JSON.stringify(
            review
              ? { expected_revision: current.revision }
              : { ...draft, expected_revision: current.revision }
          ),
        }
      )
    },
    onSuccess: (result) => {
      setSelected(result.id)
      setDraft(null)
      setFilename("")
      if (fileInput.current) fileInput.current.value = ""
      for (const key of ["reports", "report"])
        void cache.invalidateQueries({ queryKey: [key, recording.id] })
    },
  })
  const load = async (file: File | undefined) => {
    setError(null)
    setDraft(null)
    setFilename("")
    if (!file) return
    try {
      if (file.size > 1024 * 1024)
        throw new Error("Choose a draft smaller than 1 MiB")
      const value: unknown = JSON.parse(await file.text())
      if (
        !value ||
        typeof value !== "object" ||
        Array.isArray(value) ||
        !("report" in value)
      )
        throw new Error("Choose a report draft JSON from your agent")
      setDraft(value as Record<string, unknown>)
      setFilename(file.name)
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Draft could not be read"
      )
    }
  }
  if (!recording.sha256 || recording.duration === null) return null
  const saved = detail.data
  const images = [
    ...new Set(
      saved?.report.citations
        .filter((c) => c.kind === "image")
        .map((c) => c.evidence_id) ?? []
    ),
  ]
  return (
    <details className="recording-reports">
      <summary>Reports</summary>
      {canEdit && (
        <div className="report-import">
          <label>
            Import draft
            <Input
              aria-label="Import report draft"
              ref={fileInput}
              disabled={mutation.isPending}
              type="file"
              accept="application/json,.json"
              onChange={(event) => {
                void load(event.target.files?.[0])
              }}
            />
          </label>
          {filename && <p>{filename}</p>}
          <Button
            size="sm"
            disabled={!draft || mutation.isPending}
            onClick={() => mutation.mutate(false)}
          >
            Save draft
          </Button>
        </div>
      )}
      <div className="evidence-list">
        {reports.data?.reports.map((report) => (
          <Button
            key={report.id}
            size="sm"
            variant="outline"
            onClick={() => setSelected(report.id)}
          >
            {report.title} · {report.status === "draft" ? "Draft" : "Reviewed"}
            {report.stale ? " · Stale" : ""}
          </Button>
        ))}
      </div>
      {saved && (
        <article>
          <h3>{saved.title}</h3>
          <p>
            {saved.status === "draft" ? "Draft" : "Reviewed"}
            {saved.stale ? " · Source evidence changed" : ""}
          </p>
          <p className="muted">
            {saved.report.coverage === "partial"
              ? "Partial coverage"
              : "Complete coverage claimed by author"}
          </p>
          {saved.report.limitations?.map((limitation, index) => (
            <p key={index}>{limitation}</p>
          ))}
          {saved.report.items.map((item) => (
            <section key={item.id}>
              <h4>
                {item.kind} · {item.basis}
              </h4>
              <p>{item.text}</p>
              {item.owner && <p>Owner: {item.owner.text}</p>}
              {item.due && <p>Due: {item.due.text}</p>}
              <div className="evidence-list">
                {item.citation_ids?.map((id) => {
                  const citation = saved.report.citations.find(
                    (c) => c.id === id
                  )!
                  return (
                    <Button
                      key={id}
                      size="sm"
                      variant="ghost"
                      onClick={() => seek(citation.interval.start)}
                    >
                      {Math.floor(citation.interval.start / 60)}:
                      {(citation.interval.start % 60)
                        .toFixed(1)
                        .padStart(4, "0")}{" "}
                      · {citation.kind}
                    </Button>
                  )
                })}
              </div>
            </section>
          ))}
          {images.map((id) => (
            <figure key={id}>
              <img
                loading="lazy"
                alt={`Report screenshot ${id}`}
                src={
                  id.startsWith("asset:")
                    ? `/api/recordings/${recording.id}/visual-evidence/${id.slice(6)}/media`
                    : `/api/recordings/${recording.id}/media/${id.slice(6)}`
                }
              />
              <figcaption>{id}</figcaption>
            </figure>
          ))}
          <div className="evidence-list">
            <a href={`${base}/${saved.id}/export.zip`} download>
              Export ZIP
            </a>
            {canEdit && saved.status === "draft" && (
              <Button
                size="sm"
                disabled={saved.stale || mutation.isPending}
                onClick={() => mutation.mutate(true)}
              >
                Mark reviewed
              </Button>
            )}
          </div>
        </article>
      )}
      {(error || mutation.isError || reports.isError || detail.isError) && (
        <p role="alert">
          {error ??
            mutation.error?.message ??
            reports.error?.message ??
            detail.error?.message}
        </p>
      )}
    </details>
  )
}
