import { useEffect, useRef, useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FileText, Plus, Sparkles } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ValueSelect } from "@/components/value-select"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { request, type Recording, type Transcript } from "@/lib/api"
import type { components } from "@/lib/schema"

type Reports = components["schemas"]["ReportsList"]
type Detail = components["schemas"]["ReportDetail"]
type Generation = components["schemas"]["GenerationState"]
type Job = components["schemas"]["GenerationJob"]
const stamp = (t: number) =>
  `${Math.floor(t / 60)}:${Math.floor(t % 60)
    .toString()
    .padStart(2, "0")}`

export function ReportSource({
  recording,
  transcript,
  time,
  seek,
  playing,
  toggle,
}: {
  recording: Recording
  transcript?: Transcript
  time: number
  seek: (time: number) => void
  playing: boolean
  toggle: () => void
}) {
  const [expanded, setExpanded] = useState(
    () => window.matchMedia("(min-width: 960px)").matches
  )
  const transcriptPanel = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const panel = transcriptPanel.current
    const active = panel?.querySelector<HTMLElement>(".active")
    if (!panel || !active) return
    const p = panel.getBoundingClientRect(),
      a = active.getBoundingClientRect()
    if (a.top < p.top || a.bottom > p.bottom) panel.scrollTop += a.top - p.top
  }, [time, expanded])
  const frames = recording.artifacts?.filter((a) => a.kind === "frame") ?? []
  const frame = frames.filter((f) => (f.time ?? 0) <= time).at(-1) ?? frames[0]
  return (
    <details
      className="report-source"
      open={expanded}
      onToggle={(e) => setExpanded(e.currentTarget.open)}
    >
      <summary>
        View source <span>{stamp(time)}</span>
      </summary>
      <div className="report-source-body">
        {frame && (
          <img
            src={frame.url}
            alt={`Recording frame near ${stamp(frame.time ?? 0)}`}
          />
        )}
        <div className="report-source-controls">
          <Button
            size="sm"
            variant="outline"
            disabled={!recording.artifacts?.some((a) => a.kind === "audio")}
            onClick={toggle}
          >
            {playing ? "Pause" : "Play"}
          </Button>
          <input
            aria-label="Source position"
            type="range"
            min={0}
            max={recording.duration ?? 1}
            step={0.1}
            value={time}
            onChange={(e) => seek(Number(e.target.value))}
          />
          <span>{stamp(time)}</span>
        </div>
        <div
          className="report-source-transcript"
          ref={transcriptPanel}
          aria-label="Source transcript"
        >
          {transcript?.segments.map((s) => (
            <button
              key={s.id}
              className={time >= s.start && time < s.end ? "active" : ""}
              onClick={() => seek(s.start)}
            >
              <time>{stamp(s.start)}</time>
              <span>{s.text}</span>
            </button>
          ))}
        </div>
      </div>
    </details>
  )
}

export function RecordingReports({
  recording,
  canEdit,
  seek,
  source,
  pauseSource,
}: {
  recording: Recording
  canEdit: boolean
  seek: (time: number) => void
  source: ReactNode
  pauseSource: () => void
}) {
  const cache = useQueryClient()
  const base = `/api/recordings/${recording.id}/reports`
  const generationBase = `/api/recordings/${recording.id}/report-generation`
  const [selected, setSelected] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [kind, setKind] = useState("summary")
  const [previousReportId, setPreviousReportId] = useState<string | null>(null)
  const [instruction, setInstruction] = useState("")
  const [language, setLanguage] = useState(
    recording.language === "es" ? "es" : "en"
  )
  const [visuals, setVisuals] = useState(true)
  const [start, setStart] = useState(0)
  const [end, setEnd] = useState(recording.duration ?? 0)
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState("")
  const reportVideos = useRef<HTMLDivElement>(null)
  const reports = useQuery({
    queryKey: ["reports", recording.id],
    queryFn: () => request<Reports>(base),
  })
  const generation = useQuery({
    queryKey: ["report-generation", recording.id],
    queryFn: () => request<Generation>(generationBase),
    refetchInterval: (query) =>
      query.state.data?.jobs.some((j) =>
        ["queued", "running"].includes(j.state)
      )
        ? 1500
        : 10000,
  })
  const active = generation.data?.jobs.find((j) =>
    ["queued", "running"].includes(j.state)
  )
  const latest = generation.data?.jobs[0]
  const completedId = generation.data?.jobs.find(
    (j) => j.state === "completed"
  )?.report_id
  useEffect(() => {
    if (completedId)
      void cache.invalidateQueries({ queryKey: ["reports", recording.id] })
  }, [cache, completedId, recording.id])
  const selectedId = selected ?? reports.data?.reports[0]?.id
  const detail = useQuery({
    queryKey: ["report", recording.id, selectedId],
    queryFn: () => request<Detail>(`${base}/${selectedId}`),
    enabled: !!selectedId,
  })
  const refresh = () => {
    for (const key of ["reports", "report", "report-generation"])
      void cache.invalidateQueries({ queryKey: [key, recording.id] })
  }
  const generate = useMutation({
    mutationFn: () =>
      request<Job>(generationBase, {
        method: "POST",
        body: JSON.stringify({
          previous_report_id: previousReportId,
          kind,
          instruction,
          language,
          visuals,
          start,
          end,
        }),
      }),
    onSuccess: () => {
      setCreateOpen(false)
      setSelected(null)
      refresh()
    },
  })
  const cancel = useMutation({
    mutationFn: () =>
      request<Job>(`${generationBase}/${active!.id}/cancel`, {
        method: "POST",
      }),
    onSuccess: refresh,
  })
  const review = useMutation({
    mutationFn: async () => {
      const current = await request<Reports>(base)
      return request(`${base}/${selectedId}/review`, {
        method: "POST",
        body: JSON.stringify({ expected_revision: current.revision }),
      })
    },
    onSuccess: refresh,
  })
  const save = useMutation({
    mutationFn: async () => {
      const current = await request<Reports>(base)
      return request<Detail>(base, {
        method: "POST",
        body: JSON.stringify({ ...draft, expected_revision: current.revision }),
      })
    },
    onSuccess: (result) => {
      setSelected(result.id)
      setImportOpen(false)
      setDraft(null)
      refresh()
    },
  })
  const saved = detail.data
  const load = async (file?: File) => {
    setDraft(null)
    setError("")
    if (!file) return
    try {
      if (file.size > 1024 ** 2)
        throw new Error("Choose a draft smaller than 1 MiB")
      const value: unknown = JSON.parse(await file.text())
      if (
        !value ||
        typeof value !== "object" ||
        Array.isArray(value) ||
        !("report" in value)
      )
        throw new Error("Choose a report draft JSON")
      setDraft(value as Record<string, unknown>)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read draft")
    }
  }
  return (
    <div
      className="reports-workspace"
      id="recording-reports-view"
      role="tabpanel"
    >
      <section
        className="report-document recording-reports"
        aria-label="Recording reports"
      >
        <div className="report-toolbar">
          <div className="report-picker">
            <FileText size={17} />
            {reports.data?.reports.length ? (
              <ValueSelect
                label="Select report"
                value={selectedId ?? ""}
                onChange={(id) => setSelected(id)}
                items={reports.data.reports.map((r) => ({
                  value: r.id,
                  label: `${r.title} · ${new Date(r.created * 1000).toLocaleString()}${r.stale ? " · Stale" : ""}`,
                }))}
              />
            ) : (
              <strong>Reports</strong>
            )}
          </div>
          {canEdit && (
            <Button
              size="sm"
              disabled={!generation.data?.enabled || !!active}
              onClick={() => {
                generate.reset()
                setPreviousReportId(null)
                setCreateOpen(true)
              }}
            >
              <Plus size={15} /> New
            </Button>
          )}
        </div>
        {active && (
          <div className="report-progress" role="status">
            <Sparkles size={16} />
            <span>{active.stage}… You can leave this tab.</span>
            {canEdit && (
              <Button
                size="sm"
                variant="ghost"
                disabled={cancel.isPending}
                onClick={() => cancel.mutate()}
              >
                Cancel
              </Button>
            )}
          </div>
        )}
        {latest?.state === "failed" && (
          <p className="report-notice" role="alert">
            {latest.error}
          </p>
        )}
        {[
          reports.error,
          detail.error,
          generation.error,
          review.error,
          cancel.error,
        ]
          .filter(Boolean)
          .map((e, i) => (
            <p className="report-notice" role="alert" key={i}>
              {e?.message}
            </p>
          ))}
        {reports.isLoading || (selectedId && detail.isLoading) ? (
          <p className="report-empty">Opening report…</p>
        ) : saved ? (
          <article className="report-body" ref={reportVideos}>
            <header>
              <span className="report-eyebrow">
                {saved.status === "reviewed" ? "Reviewed" : "Draft"}
                {saved.stale ? " · Source changed" : ""}
              </span>
              <h1>{saved.title}</h1>
              {saved.stale && (
                <p className="report-notice">
                  This report refers to an earlier source revision. Generate a
                  new draft before reviewing it.
                </p>
              )}
            </header>
            {saved.report.limitations?.map((l, i) => (
              <p key={i} className="report-limitation">
                {l}
              </p>
            ))}
            {saved.report.items.map((item) => (
              <section className="report-section" key={item.id}>
                <h2>
                  {item.kind === "action"
                    ? "Action"
                    : item.kind === "decision"
                      ? "Decision"
                      : item.kind === "question"
                        ? "Open question"
                        : item.kind === "chapter"
                          ? "Chapter"
                          : "Key point"}
                  {item.basis !== "explicit" && <small> · {item.basis}</small>}
                </h2>
                <p>{item.text}</p>
                {(item.owner || item.due) && (
                  <p className="report-assignment">
                    {item.owner && <>Owner: {item.owner.text}</>}
                    {item.owner && item.due && " · "}
                    {item.due && <>Due: {item.due.text}</>}
                  </p>
                )}
                <div className="report-citations">
                  {item.citation_ids?.map((id) => {
                    const c = saved.report.citations.find((c) => c.id === id)!
                    return (
                      <Button
                        key={id}
                        size="sm"
                        variant="ghost"
                        onClick={() => seek(c.interval.start)}
                        aria-label={`View source at ${stamp(c.interval.start)}`}
                      >
                        {stamp(c.interval.start)} ·{" "}
                        {c.kind === "image" ? "Screen" : "Source"}
                      </Button>
                    )
                  })}
                </div>
                {item.citation_ids
                  ?.map((id) => saved.report.citations.find((c) => c.id === id))
                  .filter((c) => c?.kind === "image")
                  .map(
                    (c) =>
                      c && (
                        <figure key={c.id}>
                          <img
                            loading="lazy"
                            alt={`Supporting screenshot at ${stamp(c.interval.start)}`}
                            src={
                              c.evidence_id.startsWith("asset:")
                                ? `/api/recordings/${recording.id}/visual-evidence/${c.evidence_id.slice(6)}/media`
                                : `/api/recordings/${recording.id}/media/${c.evidence_id.slice(6)}`
                            }
                          />
                          <figcaption>
                            Source screen · {stamp(c.interval.start)}
                          </figcaption>
                        </figure>
                      )
                  )}
                {saved.report.video_references
                  ?.filter((v) => v.item_id === item.id)
                  .map((v) => (
                    <figure key={v.asset_id}>
                      <video
                        controls
                        preload="none"
                        aria-label={v.caption}
                        src={`${base}/${saved.id}/clips/${v.asset_id}`}
                        onPlay={(event) => {
                          pauseSource()
                          reportVideos.current
                            ?.querySelectorAll("video")
                            .forEach((video) => {
                              if (video !== event.currentTarget) video.pause()
                            })
                        }}
                      />
                      <figcaption>
                        {v.caption} · {stamp(v.interval.start)}–
                        {stamp(v.interval.end)}
                      </figcaption>
                    </figure>
                  ))}
              </section>
            ))}
            <footer className="report-footer">
              {canEdit && generation.data?.enabled && (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={!!active || saved.stale}
                  onClick={() => {
                    generate.reset()
                    setKind("custom")
                    setInstruction("")
                    setPreviousReportId(saved.id)
                    setStart(saved.report.scope.start)
                    setEnd(saved.report.scope.end)
                    setCreateOpen(true)
                  }}
                >
                  Refine
                </Button>
              )}
              <a href={`${base}/${saved.id}/export.zip`} download>
                Export report
              </a>
              {canEdit && saved.status === "draft" && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={saved.stale || review.isPending}
                  onClick={() => review.mutate()}
                >
                  Mark reviewed
                </Button>
              )}
            </footer>
            <details className="report-provenance">
              <summary>Report details</summary>
              <p>
                {saved.generator.name} ·{" "}
                {saved.generator.model ?? "Model unspecified"} ·{" "}
                {saved.report.coverage === "complete"
                  ? "Full transcript coverage"
                  : "Partial coverage"}
              </p>
              <p>
                Review is an editor action. Model interpretation can be
                incorrect. Exports include selected images and video references;
                video playback stays in the app.
              </p>
            </details>
          </article>
        ) : (
          <div className="report-empty">
            <FileText size={32} />
            <h2>Turn this recording into something useful.</h2>
            <p>
              Create a summary, an action list, or a report with supporting
              visuals.
            </p>
            {!generation.data?.enabled && canEdit && (
              <p>Report generation is not configured on this server yet.</p>
            )}
            {!canEdit && <p>Reports created by your team will appear here.</p>}
            {canEdit && generation.data?.enabled && !active && (
              <Button
                onClick={() => {
                  generate.reset()
                  setPreviousReportId(null)
                  setCreateOpen(true)
                }}
              >
                <Sparkles size={15} /> Create a report
              </Button>
            )}
          </div>
        )}
        {canEdit && (
          <details className="report-advanced">
            <summary>More options</summary>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                save.reset()
                setImportOpen(true)
              }}
            >
              Import draft
            </Button>
          </details>
        )}
      </section>
      {source}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="report-create">
          <DialogHeader>
            <DialogTitle>
              {previousReportId ? "Refine report" : "Create a report"}
            </DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              generate.mutate()
            }}
          >
            <label>
              What would you like?
              <ValueSelect
                label="Report type"
                value={kind}
                onChange={setKind}
                items={[
                  { value: "summary", label: "Summary" },
                  { value: "actions", label: "Action list" },
                  { value: "report", label: "Visual report" },
                  { value: "custom", label: "Custom request" },
                ]}
              />
            </label>
            <label>
              Anything to focus on? <span className="muted">Optional</span>
              <textarea
                maxLength={2000}
                rows={3}
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                placeholder="For example: explain the demo and highlight next steps…"
              />
            </label>
            <label>
              Language
              <ValueSelect
                label="Report language"
                value={language}
                onChange={setLanguage}
                items={[
                  { value: "en", label: "English" },
                  { value: "es", label: "Spanish" },
                ]}
              />
            </label>
            <label className="report-check">
              <input
                type="checkbox"
                checked={visuals}
                onChange={(e) => setVisuals(e.target.checked)}
              />{" "}
              Include relevant screenshots and video references
            </label>
            <details>
              <summary>Choose a time range</summary>
              <div className="report-range">
                <label>
                  From (seconds)
                  <Input
                    type="number"
                    min={0}
                    max={recording.duration ?? 0}
                    step="0.1"
                    value={start}
                    onChange={(e) => setStart(Number(e.target.value))}
                  />
                </label>
                <label>
                  To (seconds)
                  <Input
                    type="number"
                    min={start}
                    max={recording.duration ?? 0}
                    step="0.1"
                    value={end}
                    onChange={(e) => setEnd(Number(e.target.value))}
                  />
                </label>
              </div>
            </details>
            <p className="muted">
              Selected transcript passages and inspected images are sent to
              OpenAI. The result is saved as a draft.
            </p>
            {generate.error && <p role="alert">{generate.error.message}</p>}
            <Button
              type="submit"
              disabled={generate.isPending || !(end > start)}
            >
              {generate.isPending ? "Queuing…" : "Generate draft"}
            </Button>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog open={importOpen} onOpenChange={setImportOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Import report draft</DialogTitle>
          </DialogHeader>
          <Input
            aria-label="Import report draft"
            type="file"
            accept="application/json,.json"
            onChange={(e) => void load(e.target.files?.[0])}
          />
          {(error || save.error) && (
            <p role="alert">{error || save.error?.message}</p>
          )}
          <Button
            disabled={!draft || save.isPending}
            onClick={() => save.mutate()}
          >
            Save draft
          </Button>
        </DialogContent>
      </Dialog>
    </div>
  )
}
