import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { ValueSelect } from "@/components/value-select"
import { request, type Recording, type Health } from "@/lib/api"
import type { components } from "@/lib/schema"

export type Speakers = components["schemas"]["SpeakersSnapshot"]
type Evidence = components["schemas"]["EvidenceSnapshot"]
const stamp = (t: number) =>
  `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`

export function SpeakerReview({
  recording,
  state,
  canEdit,
  time,
  seek,
  filter,
  setFilter,
  health,
}: {
  recording: Recording
  state?: Speakers
  canEdit: boolean
  time: number
  seek: (t: number) => void
  filter: string
  setFilter: (v: string) => void
  health?: Health
}) {
  const [editorOpen, setEditorOpen] = useState(false)
  const cache = useQueryClient()
  const base = `/api/recordings/${recording.id}`
  const evidence = useQuery({
    queryKey: ["evidence", recording.id],
    queryFn: () => request<Evidence>(base + "/evidence"),
  })
  const [start, setStart] = useState(0),
    [end, setEnd] = useState(0)
  const [target, setTarget] = useState("unknown")
  const [min, setMin] = useState(1),
    [max, setMax] = useState(16)
  const [renames, setRenames] = useState<Record<string, string>>({})
  const [newName, setNewName] = useState("")
  const change = useMutation({
    mutationFn: async ({
      action,
      id,
    }: {
      action:
        | "create"
        | "rename"
        | "assign"
        | "reset"
        | "undo"
        | "analyze"
        | "cancel"
        | "more"
      id?: string
    }) => {
      if (action === "more") {
        const query = new URLSearchParams({
          passage_offset: String(
            state!.next_passage_offset ?? state!.passages.length
          ),
          turn_offset: String(state!.next_turn_offset ?? state!.turns.length),
          expected_revision: String(state!.revision),
          expected_transcript_revision: String(state!.transcript_revision),
        })
        const next = await request<Speakers>(base + "/speakers?" + query)
        cache.setQueryData(["speakers", recording.id], {
          ...next,
          passages: [...state!.passages, ...next.passages],
          turns: [...state!.turns, ...next.turns],
        })
        return
      }
      const expected = {
        expected_revision: state?.revision ?? 0,
        expected_source_sha256: evidence.data?.source_sha256,
      }
      if (action === "create") {
        const speaker = await request<{ id: string }>(base + "/speakers", {
          method: "POST",
          body: JSON.stringify({ ...expected, label: newName.trim() }),
        })
        setNewName("")
        setTarget(speaker.id)
        return
      }
      if (action === "analyze")
        return request(base + "/diarization", {
          method: "POST",
          body: JSON.stringify({
            reference: {
              recording_id: recording.id,
              source_sha256: evidence.data!.source_sha256,
              interval: { start: 0, end: recording.duration },
              versions: evidence.data!.versions,
            },
            options: { min_speakers: min, max_speakers: max },
          }),
        })
      if (action === "cancel")
        return request(
          base + `/diarization/${recording.speaker_job!.id}/cancel`,
          { method: "POST" }
        )
      const path =
        action === "rename"
          ? `/speakers/${id}`
          : action === "assign"
            ? "/speaker-overrides"
            : action === "reset"
              ? `/speaker-overrides/${id}/reset`
              : "/speakers/undo"
      return request(base + path, {
        method: action === "rename" ? "PUT" : "POST",
        body: JSON.stringify({
          ...expected,
          ...(action === "rename"
            ? {
                label:
                  renames[id!] ??
                  state!.speakers.find((s) => s.id === id)!.label,
              }
            : {}),
          ...(action === "assign"
            ? {
                override: {
                  interval: { start, end },
                  speaker: target === "unknown" ? null : target,
                },
              }
            : {}),
        }),
      })
    },
    onSuccess: (_, variables) => {
      if (variables.action !== "more") {
        void cache.invalidateQueries({ queryKey: ["speakers", recording.id] })
        void cache.invalidateQueries({ queryKey: ["evidence", recording.id] })
        void cache.invalidateQueries({
          queryKey: ["recordings", recording.project_id],
        })
      }
    },
    onError: () => {
      void cache.invalidateQueries({ queryKey: ["speakers", recording.id] })
      void cache.invalidateQueries({ queryKey: ["evidence", recording.id] })
    },
  })
  const busy = change.isPending || !state || !evidence.data?.source_sha256
  const job = recording.speaker_job
  const active = job?.state === "queued" || job?.state === "running"
  const names = state?.speakers ?? []
  const rangeValid =
    Number.isFinite(start) &&
    Number.isFinite(end) &&
    start >= 0 &&
    end > start &&
    end <= (recording.duration ?? 0)
  return (
    <section className="speaker-review" aria-label="Speakers">
      <div className="speaker-actions">
        <h3>Speakers</h3>
        <ValueSelect
          label="Filter speaker"
          value={filter}
          items={[
            { value: "all", label: "All speakers" },
            { value: "unknown", label: "Unknown" },
            ...names.map((s) => ({ value: s.id, label: s.label })),
          ]}
          onChange={setFilter}
        />
      </div>
      {job && (
        <p role="status">
          {job.state === "running"
            ? "Analyzing speakers…"
            : job.state === "queued"
              ? "Speaker analysis queued"
              : job.state === "failed"
                ? job.error
                : job.state === "cancelled"
                  ? "Speaker analysis cancelled"
                  : "Speaker analysis complete"}
        </p>
      )}
      {state?.stale && (
        <p role="status">
          Speaker analysis needs refreshing after transcript changes.
        </p>
      )}
      {canEdit && (
        <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setStart(time)
              setEnd(Math.min(time + 10, recording.duration ?? 0))
              setEditorOpen(true)
            }}
          >
            Edit speakers
          </Button>
          <DialogContent
            className="speaker-dialog"
            aria-describedby={undefined}
          >
            <DialogHeader>
              <DialogTitle>Edit speakers</DialogTitle>
            </DialogHeader>
            <p>
              Automatic labels are experimental. Add or correct speakers
              manually.
            </p>
            <form
              className="speaker-actions"
              onSubmit={(e) => {
                e.preventDefault()
                if (newName.trim()) change.mutate({ action: "create" })
              }}
            >
              <label>
                New speaker
                <Input
                  aria-label="New speaker name"
                  value={newName}
                  maxLength={80}
                  onChange={(e) => setNewName(e.target.value)}
                />
              </label>
              <Button
                type="submit"
                size="sm"
                disabled={busy || !newName.trim()}
              >
                Add speaker
              </Button>
            </form>
            <div className="speaker-actions">
              <label>
                Min speakers
                <Input
                  aria-label="Minimum speakers"
                  type="number"
                  min={1}
                  max={32}
                  value={min}
                  onChange={(e) => setMin(Number(e.target.value))}
                />
              </label>
              <label>
                Max speakers
                <Input
                  aria-label="Maximum speakers"
                  type="number"
                  min={min}
                  max={32}
                  value={max}
                  onChange={(e) => setMax(Number(e.target.value))}
                />
              </label>
              <Button
                size="sm"
                disabled={
                  busy ||
                  active ||
                  !health?.diarization_available ||
                  !Number.isInteger(min) ||
                  !Number.isInteger(max) ||
                  min < 1 ||
                  max < min ||
                  max > 32
                }
                onClick={() => change.mutate({ action: "analyze" })}
              >
                Analyze speakers
              </Button>
              {active && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => change.mutate({ action: "cancel" })}
                >
                  Cancel analysis
                </Button>
              )}
            </div>
            {!health?.diarization_available && (
              <p>No speaker-analysis worker is available.</p>
            )}
            {names.map((s) => (
              <form
                className="speaker-actions"
                key={s.id}
                onSubmit={(e) => {
                  e.preventDefault()
                  change.mutate({ action: "rename", id: s.id })
                }}
              >
                <label>
                  {s.label}
                  <Input
                    aria-label={`Name for ${s.label}`}
                    value={renames[s.id] ?? s.label}
                    maxLength={80}
                    onChange={(e) =>
                      setRenames({ ...renames, [s.id]: e.target.value })
                    }
                  />
                </label>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy || !(renames[s.id] ?? s.label).trim()}
                  type="submit"
                >
                  Save name
                </Button>
              </form>
            ))}
            {
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  if (rangeValid) change.mutate({ action: "assign" })
                }}
              >
                <div className="speaker-actions">
                  <label>
                    From (s)
                    <Input
                      aria-label="Speaker range start"
                      type="number"
                      min={0}
                      step="any"
                      value={start}
                      onChange={(e) => setStart(Number(e.target.value))}
                    />
                  </label>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setStart(time)
                      if (end <= time)
                        setEnd(Math.min(recording.duration ?? time, time + 1))
                    }}
                  >
                    From here
                  </Button>
                  <label>
                    To (s)
                    <Input
                      aria-label="Speaker range end"
                      type="number"
                      min={start}
                      max={recording.duration ?? 0}
                      step="any"
                      value={end}
                      onChange={(e) => setEnd(Number(e.target.value))}
                    />
                  </label>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => setEnd(time)}
                  >
                    To here
                  </Button>
                </div>
                <div className="speaker-actions">
                  <ValueSelect
                    label="Assign speaker"
                    value={target}
                    onChange={setTarget}
                    items={[
                      { value: "unknown", label: "Unknown" },
                      ...names.map((s) => ({ value: s.id, label: s.label })),
                    ]}
                  />
                  <Button
                    type="submit"
                    size="sm"
                    disabled={busy || !rangeValid}
                  >
                    Assign range
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => change.mutate({ action: "undo" })}
                  >
                    Undo speaker edit
                  </Button>
                </div>
              </form>
            }
          </DialogContent>
        </Dialog>
      )}
      {!!state?.overrides.length && (
        <ul>
          {state.overrides.slice(-100).map((o) => (
            <li key={o.id}>
              <button onClick={() => seek(o.interval.start)}>
                {stamp(o.interval.start)}–{stamp(o.interval.end)} ·{" "}
                {names.find((s) => s.id === o.speaker)?.label ?? "Unknown"}
              </button>
              {canEdit && (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => change.mutate({ action: "reset", id: o.id })}
                >
                  Reset range
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {(state?.next_passage_offset !== null ||
        state?.next_turn_offset !== null) &&
        state && (
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => change.mutate({ action: "more" })}
          >
            Load more speaker data
          </Button>
        )}
      {change.isError && <p role="alert">{change.error.message}</p>}
    </section>
  )
}
