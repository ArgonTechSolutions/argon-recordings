import { useEffect, useRef, useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, FileText, LoaderCircle, Plus, Sparkles } from "lucide-react"
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
type Estimate = components["schemas"]["GenerationEstimate"]
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
  const [focus, setFocus] = useState("")
  const [formError, setFormError] = useState("")
  const [submitted, setSubmitted] = useState<Job | null>(null)
  const instructionInput = useRef<HTMLTextAreaElement>(null)
  const rangeDetails = useRef<HTMLDetailsElement>(null)
  const statusPanel = useRef<HTMLDivElement>(null)
  const [language, setLanguage] = useState(
    recording.language === "es" ? "es" : "en"
  )
  const [visuals, setVisuals] = useState(false)
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
  const latest = submitted
    ? (generation.data?.jobs.find((job) => job.id === submitted.id) ??
      submitted)
    : generation.data?.jobs[0]
  const active =
    latest && ["queued", "running"].includes(latest.state)
      ? latest
      : generation.data?.jobs.find((job) =>
          ["queued", "running"].includes(job.state)
        )
  const completedId = latest?.state === "completed" ? latest.report_id : null
  useEffect(() => {
    if (completedId)
      void cache.invalidateQueries({ queryKey: ["reports", recording.id] })
  }, [cache, completedId, recording.id])
  useEffect(() => {
    if (submitted)
      statusPanel.current?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      })
  }, [submitted])
  const selectedId = selected ?? completedId ?? reports.data?.reports[0]?.id
  const rangeError =
    !Number.isFinite(start) ||
    !Number.isFinite(end) ||
    start < 0 ||
    end <= start ||
    end > (recording.duration ?? 0)
      ? "Choose a time range inside the recording, with the end after the start."
      : ""
  const estimate = useQuery({
    queryKey: [
      "report-estimate",
      recording.id,
      kind,
      language,
      visuals,
      start,
      end,
    ],
    queryFn: () =>
      request<Estimate>(`${generationBase}/estimate`, {
        method: "POST",
        body: JSON.stringify({ kind, language, visuals, start, end }),
      }),
    enabled: createOpen && !rangeError,
    staleTime: 0,
    retry: false,
  })
  const rememberJob = (job: Job) => {
    cache.setQueryData<Generation>(
      ["report-generation", recording.id],
      (previous) =>
        previous
          ? {
              ...previous,
              jobs: [
                job,
                ...previous.jobs.filter((item) => item.id !== job.id),
              ],
            }
          : previous
    )
  }
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
          instruction: kind === "custom" ? instruction.trim() : "",
          focus: focus.trim(),
          language,
          visuals,
          start,
          end,
        }),
      }),
    onMutate: async () => {
      await cache.cancelQueries({
        queryKey: ["report-generation", recording.id],
      })
    },
    onSuccess: (job) => {
      setSubmitted(job)
      rememberJob(job)
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
    onSuccess: (job) => {
      rememberJob(job)
      if (submitted?.id === job.id) setSubmitted(job)
      refresh()
    },
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
                setFormError("")
                setPreviousReportId(null)
                setCreateOpen(true)
              }}
            >
              <Plus size={15} /> New
            </Button>
          )}
        </div>
        {active && (
          <div
            className="report-progress"
            role="status"
            aria-live="polite"
            ref={statusPanel}
          >
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
            <span>
              <strong>
                {active.state === "queued"
                  ? "Report queued"
                  : "Generating report"}
              </strong>
              <br />
              {active.stage}. Your draft will appear here when it is ready.
            </span>
            {canEdit && (
              <Button
                size="sm"
                variant="ghost"
                disabled={cancel.isPending}
                onClick={() => cancel.mutate()}
              >
                {cancel.isPending ? "Cancelling…" : "Cancel"}
              </Button>
            )}
          </div>
        )}
        {latest?.warning && (
          <p className="report-notice" role="status">
            {latest.warning}
          </p>
        )}
        {!active && latest?.state === "completed" && (
          <div
            className="report-progress"
            role="status"
            aria-live="polite"
            ref={statusPanel}
          >
            <Check size={16} aria-hidden="true" />
            <span>
              <strong>Your draft is ready.</strong> Review it before marking it
              reviewed.
            </span>
            {latest.report_id && selectedId !== latest.report_id && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setSelected(latest.report_id)}
              >
                Open draft
              </Button>
            )}
          </div>
        )}
        {!active && latest?.state === "cancelled" && (
          <div className="report-notice" role="status" ref={statusPanel}>
            Generation cancelled. No new draft was saved.
          </div>
        )}
        {latest?.state === "failed" && (
          <div className="report-notice" role="alert" ref={statusPanel}>
            <strong>Report could not be created.</strong>
            <p>
              {latest.error ?? "Generation stopped before a draft was saved."}
            </p>
            {latest.estimated_cost_usd > 0 && (
              <p>
                Estimated API usage: ${latest.estimated_cost_usd.toFixed(4)}. No
                draft was saved.
              </p>
            )}
            {canEdit && (
              <Button
                size="sm"
                variant="outline"
                disabled={!!active}
                onClick={() => {
                  generate.reset()
                  setFormError("")
                  setCreateOpen(true)
                }}
              >
                Edit request
              </Button>
            )}
          </div>
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
                    setFormError("")
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
            <h2>
              {active
                ? "Your report is being created."
                : latest?.state === "failed"
                  ? "No draft was saved."
                  : "Turn this recording into something useful."}
            </h2>
            <p>
              {active
                ? "You can leave this tab and return while generation continues."
                : latest?.state === "failed"
                  ? "You can edit your request above and try again when you are ready."
                  : "Create a summary, an action list, or a report with supporting visuals."}
            </p>
            {!generation.data?.enabled && canEdit && (
              <p>Report generation is not configured on this server yet.</p>
            )}
            {!canEdit && <p>Reports created by your team will appear here.</p>}
            {canEdit &&
              generation.data?.enabled &&
              !active &&
              latest?.state !== "failed" && (
                <Button
                  onClick={() => {
                    generate.reset()
                    setFormError("")
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
      <Dialog
        open={createOpen}
        onOpenChange={(open) => {
          if (!generate.isPending) setCreateOpen(open)
        }}
      >
        <DialogContent className="report-create">
          <DialogHeader>
            <DialogTitle>
              {previousReportId ? "Refine report" : "Create a report"}
            </DialogTitle>
          </DialogHeader>
          <form
            noValidate
            aria-busy={generate.isPending}
            onSubmit={(e) => {
              e.preventDefault()
              if (generate.isPending) return
              setFormError("")
              if (kind === "custom" && !instruction.trim()) {
                setFormError("Describe what this report should do.")
                instructionInput.current?.focus()
                return
              }
              if (rangeError) {
                setFormError(rangeError)
                if (rangeDetails.current) rangeDetails.current.open = true
                return
              }
              generate.mutate()
            }}
          >
            <label>
              What would you like?
              <ValueSelect
                label="Report type"
                value={kind}
                disabled={generate.isPending}
                onChange={(value) => {
                  setKind(value)
                  setVisuals(value === "report")
                }}
                items={[
                  { value: "summary", label: "Summary" },
                  { value: "actions", label: "Action list" },
                  { value: "report", label: "Visual report" },
                  { value: "custom", label: "Custom request" },
                ]}
              />
            </label>
            {kind === "custom" && (
              <label>
                What should this report do?
                <textarea
                  ref={instructionInput}
                  aria-label="What should this report do?"
                  required
                  maxLength={2000}
                  rows={4}
                  value={instruction}
                  disabled={generate.isPending}
                  onChange={(e) => setInstruction(e.target.value)}
                  placeholder="For example: write a step-by-step implementation guide with decisions, trade-offs, and unresolved questions."
                />
              </label>
            )}
            <label>
              Anything to focus on? <span className="muted">Optional</span>
              <textarea
                aria-label="Anything to focus on?"
                maxLength={2000}
                rows={3}
                value={focus}
                disabled={generate.isPending}
                onChange={(e) => setFocus(e.target.value)}
                placeholder="For example: explain the demo and highlight next steps…"
              />
            </label>
            <label>
              Language
              <ValueSelect
                label="Report language"
                value={language}
                disabled={generate.isPending}
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
                disabled={generate.isPending}
                onChange={(e) => setVisuals(e.target.checked)}
              />{" "}
              Include relevant screenshots and video references
            </label>
            <details ref={rangeDetails}>
              <summary>Choose a time range</summary>
              <div className="report-range">
                <label>
                  From (seconds)
                  <Input
                    type="number"
                    min={0}
                    max={recording.duration ?? 0}
                    step="any"
                    disabled={generate.isPending}
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
                    step="any"
                    disabled={generate.isPending}
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
            <div className="report-notice" role="status" aria-live="polite">
              {estimate.isFetching ? (
                "Estimating cost…"
              ) : estimate.data ? (
                <>
                  <strong>
                    Estimated cost: $
                    {estimate.data.estimated_cost_usd.toFixed(3)}
                  </strong>
                  <p className="muted">
                    Typical range ${estimate.data.low_usd.toFixed(3)}–$
                    {estimate.data.high_usd.toFixed(3)}. Actual usage may vary.
                    This estimate is not a spending cap.
                  </p>
                  {estimate.data.cached_batches > 0 && (
                    <p className="muted">
                      Reusing saved transcript readings to reduce cost.
                    </p>
                  )}
                  {estimate.data.allowance_warning && (
                    <p>
                      This may exceed the server’s monthly cost guideline.
                      Generation can still continue.
                    </p>
                  )}
                  {estimate.data.hard_limit_warning && (
                    <p>
                      A configured hard limit may prevent this report from
                      finishing. Ask an administrator to review the limits.
                    </p>
                  )}
                </>
              ) : estimate.error ? (
                "Cost estimate unavailable. You can still generate a draft; actual usage will be shown."
              ) : (
                "Choose a valid time range to estimate cost."
              )}
            </div>
            {formError && <p role="alert">{formError}</p>}
            {generate.error && (
              <p role="alert">
                Could not queue the report: {generate.error.message}. Your
                request has been kept.
              </p>
            )}
            {generate.isPending && (
              <p role="status" aria-live="polite">
                Sending your request…
              </p>
            )}
            <Button type="submit" disabled={generate.isPending || !!active}>
              {generate.isPending && (
                <LoaderCircle className="spin" size={15} aria-hidden="true" />
              )}
              {generate.isPending ? "Sending…" : "Generate draft"}
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
