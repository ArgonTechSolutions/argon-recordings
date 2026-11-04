import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ValueSelect } from "@/components/value-select"
import { request, type Recording } from "@/lib/api"
import type { components } from "@/lib/schema"
import { VisualAnalysis } from "@/components/visual-analysis"

type Asset = components["schemas"]["VisualAsset"]
type Job = components["schemas"]["VisualJob"]
type Evidence = components["schemas"]["EvidenceSnapshot"]
type Marks = components["schemas"]["MarksSnapshot"]
const stamp = (t: number) =>
  `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, "0")}`

export function VisualEvidence({
  recording,
  canEdit,
  time,
  playing,
  speed,
  seek,
}: {
  recording: Recording
  canEdit: boolean
  time: number
  playing: boolean
  speed: number
  seek: (time: number) => void
}) {
  const cache = useQueryClient()
  const base = `/api/recordings/${recording.id}`
  const [kind, setKind] = useState("image"),
    [quality, setQuality] = useState("1920")
  const [start, setStart] = useState(time),
    [end, setEnd] = useState(Math.min(time + 10, recording.duration ?? 0))
  const [fps, setFps] = useState("15"),
    [selected, setSelected] = useState<Asset | null>(null)
  const [limit, setLimit] = useState(20),
    [zoom, setZoom] = useState(false)
  const video = useRef<HTMLVideoElement>(null)
  const evidence = useQuery({
    queryKey: ["evidence", recording.id],
    queryFn: () => request<Evidence>(base + "/evidence"),
  })
  const marks = useQuery({
    queryKey: ["marks", recording.id],
    queryFn: () => request<Marks>(base + "/marks"),
  })
  const assets = useQuery({
    queryKey: ["visual-evidence", recording.id],
    queryFn: () => request<Asset[]>(base + "/visual-evidence?limit=100"),
  })
  const jobs = useQuery({
    queryKey: ["visual-jobs", recording.id],
    queryFn: () => request<Job[]>(base + "/visual-jobs"),
  })
  const mutation = useMutation({
    mutationFn: async (cancelId?: string) => {
      if (cancelId)
        return request<Job>(`${base}/visual-jobs/${cancelId}/cancel`, {
          method: "POST",
        })
      const state = await request<Evidence>(base + "/evidence")
      return request<Job>(base + "/visual-jobs", {
        method: "POST",
        body: JSON.stringify({
          reference: {
            recording_id: recording.id,
            source_sha256: state.source_sha256,
            versions: state.versions,
            interval: { start: 0, end: state.duration },
          },
          recipe: {
            kind,
            start,
            end: kind === "image" ? start : end,
            max_width: quality === "source" ? 1920 : Number(quality),
            source_resolution: quality === "source",
            fps: kind === "clip" ? Number(fps) : 15,
          },
        }),
      })
    },
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["visual-jobs", recording.id] })
      void cache.invalidateQueries({ queryKey: ["evidence", recording.id] })
    },
  })
  useEffect(() => {
    for (const key of ["visual-jobs", "visual-evidence", "evidence"])
      void cache.invalidateQueries({ queryKey: [key, recording.id] })
  }, [
    cache,
    recording.id,
    recording.visual_job?.id,
    recording.visual_job?.state,
  ])

  // Original audio is the only playback clock. The clip is muted and follows
  // its source offset, including after seeking, buffering and speed changes.
  const synchronize = () => {
    const element = video.current
    if (!element || selected?.manifest.kind !== "clip") return
    const local = time - selected.manifest.actual_start
    element.playbackRate = speed
    if (local < 0 || time >= selected.manifest.actual_end) {
      element.pause()
      return
    }
    if (Number.isFinite(element.duration) && local >= element.duration - 0.02) {
      element.pause()
      return
    }
    if (
      Number.isFinite(element.duration) &&
      Math.abs(element.currentTime - local) > 0.25
    )
      element.currentTime = Math.min(
        local,
        Math.max(0, element.duration - 0.01)
      )
    if (playing) void element.play().catch(() => {})
    else element.pause()
  }
  useEffect(synchronize, [time, playing, speed, selected])
  const choose = (asset: Asset) => {
    setSelected(asset)
    setZoom(false)
    seek(asset.manifest.actual_start)
  }
  const busy =
    jobs.data?.filter((j) => j.state === "queued" || j.state === "running") ??
    []
  const videoAvailable = recording.artifacts?.some((a) => a.kind === "frame")
  return (
    <div className="visual-evidence">
      <h3>Visual evidence</h3>
      {canEdit && videoAvailable && (
        <section>
          <h3>Extract evidence</h3>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              mutation.mutate(undefined)
            }}
          >
            <ValueSelect
              label="Evidence type"
              value={kind}
              onChange={(value) => {
                setKind(value)
                if (value === "clip" && quality === "source") setQuality("1920")
              }}
              items={[
                { value: "image", label: "Screenshot" },
                { value: "clip", label: "Motion clip" },
              ]}
            />
            <div className="evidence-range">
              <label>
                Start
                <Input
                  aria-label="Evidence start"
                  type="number"
                  min={0}
                  max={recording.duration ?? 0}
                  step="any"
                  value={start}
                  onChange={(e) => setStart(Number(e.target.value))}
                  required
                />
              </label>
              {kind === "clip" && (
                <label>
                  End
                  <Input
                    aria-label="Evidence end"
                    type="number"
                    min={start + 0.01}
                    max={Math.min(start + 30, recording.duration ?? 0)}
                    step="any"
                    value={end}
                    onChange={(e) => setEnd(Number(e.target.value))}
                    required
                  />
                </label>
              )}
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => {
                  setStart(
                    Math.min(
                      time,
                      Math.max(0, (recording.duration ?? 0) - 0.01)
                    )
                  )
                  setEnd(Math.min(time + 10, recording.duration ?? 0))
                }}
              >
                Use playhead
              </Button>
            </div>
            {!!marks.data?.marks.length && (
              <ValueSelect
                label="Use timeline mark"
                value="none"
                onChange={(id) => {
                  const mark = marks.data?.marks.find((m) => m.id === id)
                  if (mark) {
                    setStart(mark.interval.start)
                    setEnd(mark.interval.end)
                    setKind(
                      mark.policy === "motion" &&
                        mark.interval.end > mark.interval.start
                        ? "clip"
                        : "image"
                    )
                  }
                }}
                items={[
                  { value: "none", label: "Use a mark…" },
                  ...(marks.data?.marks.map((m) => ({
                    value: m.id,
                    label: m.label || stamp(m.interval.start),
                  })) ?? []),
                ]}
              />
            )}
            <div className="evidence-range">
              <ValueSelect
                label="Evidence quality"
                value={quality}
                onChange={setQuality}
                items={[
                  { value: "1280", label: "1280 px" },
                  { value: "1920", label: "1920 px" },
                  ...(kind === "image"
                    ? [{ value: "source", label: "Source detail" }]
                    : []),
                ]}
              />
              {kind === "clip" && (
                <ValueSelect
                  label="Clip frame rate"
                  value={fps}
                  onChange={setFps}
                  items={[
                    { value: "5", label: "5 fps" },
                    { value: "15", label: "15 fps" },
                    { value: "30", label: "30 fps" },
                  ]}
                />
              )}
              <Button
                type="submit"
                size="sm"
                disabled={
                  mutation.isPending || !evidence.data?.source_available
                }
              >
                Extract
              </Button>
            </div>
          </form>
          {!evidence.data?.source_available && (
            <p>Original source unavailable.</p>
          )}
        </section>
      )}
      {mutation.isError && <p role="alert">{mutation.error.message}</p>}
      {busy.map((job) => (
        <div className="evidence-job" key={job.id}>
          <span>{job.state === "queued" ? "Visual queued" : "Extracting"}</span>
          {canEdit && (
            <Button
              size="sm"
              variant="ghost"
              disabled={mutation.isPending}
              onClick={() => mutation.mutate(job.id)}
            >
              Cancel
            </Button>
          )}
        </div>
      ))}
      {jobs.data?.[0]?.state === "failed" && (
        <p role="alert">{jobs.data[0].error}</p>
      )}
      {selected && (
        <div className="evidence-preview">
          {selected.manifest.kind === "image" ? (
            <button
              className="evidence-image"
              onClick={() => setZoom(!zoom)}
              aria-label="Zoom evidence screenshot"
            >
              <img
                className={zoom ? "zoomed-evidence" : ""}
                src={selected.url}
                alt={`Screenshot at ${stamp(selected.manifest.actual_start)}`}
              />
            </button>
          ) : (
            <video
              ref={video}
              key={selected.id}
              src={selected.url}
              muted
              playsInline
              preload="metadata"
              onLoadedMetadata={synchronize}
              onCanPlay={synchronize}
              aria-label="Synchronized evidence clip"
            />
          )}
          <p>
            Source {stamp(selected.manifest.actual_start)}
            {selected.manifest.kind === "clip"
              ? `–${stamp(selected.manifest.actual_end)}`
              : ""}{" "}
            · {selected.manifest.width} × {selected.manifest.height}
          </p>
          {selected.manifest.kind === "clip" && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => seek(selected.manifest.actual_start)}
            >
              Go to clip start
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => setSelected(null)}>
            Close evidence
          </Button>
        </div>
      )}
      <div className="evidence-list">
        {assets.data?.slice(0, limit).map((asset) => (
          <Button
            key={asset.id}
            variant={selected?.id === asset.id ? "secondary" : "outline"}
            size="sm"
            onClick={() => choose(asset)}
          >
            {asset.manifest.kind === "image" ? "Still" : "Clip"}{" "}
            {stamp(asset.manifest.actual_start)}
          </Button>
        ))}
      </div>
      {(assets.data?.length ?? 0) > limit && (
        <Button size="sm" variant="ghost" onClick={() => setLimit(limit + 20)}>
          More evidence
        </Button>
      )}
      {assets.isError && <p role="alert">{assets.error.message}</p>}
      <VisualAnalysis recording={recording} canEdit={canEdit} seek={seek} />
    </div>
  )
}
