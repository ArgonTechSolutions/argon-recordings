import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ValueSelect } from "@/components/value-select"
import { request } from "@/lib/api"
import type { components } from "@/lib/schema"

type Snapshot = components["schemas"]["EvidenceSnapshot"]
type Marks = components["schemas"]["MarksSnapshot"]
type Mark = components["schemas"]["MarkView"]
const stamp = (t: number) =>
  `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`

export function VisualMarks({
  recordingId,
  time,
  duration,
  canEdit,
  seek,
}: {
  recordingId: string
  time: number
  duration: number
  canEdit: boolean
  seek: (time: number) => void
}) {
  const cache = useQueryClient()
  const base = `/api/recordings/${recordingId}`
  const key = ["marks", recordingId]
  const marks = useQuery({
    queryKey: key,
    queryFn: () => request<Marks>(`${base}/marks`),
  })
  const evidence = useQuery({
    queryKey: ["evidence", recordingId],
    queryFn: () => request<Snapshot>(`${base}/evidence`),
  })
  const [start, setStart] = useState(0)
  const [end, setEnd] = useState(0)
  const [label, setLabel] = useState("")
  const [policy, setPolicy] = useState<Mark["policy"]>("auto")
  const [pinned, setPinned] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const change = useMutation({
    mutationFn: async ({
      action,
      id,
    }: {
      action: "save" | "delete" | "undo"
      id?: string
    }) => {
      const expected = {
        expected_revision: marks.data!.revision,
        expected_source_sha256: evidence.data!.source_sha256,
      }
      const path =
        action === "save"
          ? editing
            ? `${base}/marks/${editing}`
            : `${base}/marks`
          : action === "delete"
            ? `${base}/marks/${id}/delete`
            : `${base}/marks/undo`
      return request<Marks>(path, {
        method: action === "save" && editing ? "PUT" : "POST",
        body: JSON.stringify({
          ...expected,
          ...(action === "save"
            ? { mark: { interval: { start, end }, label, policy, pinned } }
            : {}),
        }),
      })
    },
    onSuccess: (value) => {
      cache.setQueryData(key, value)
      setEditing(null)
      setLabel("")
      void cache.invalidateQueries({ queryKey: ["evidence", recordingId] })
    },
    onError: () => {
      void cache.invalidateQueries({ queryKey: key })
      void cache.invalidateQueries({ queryKey: ["evidence", recordingId] })
    },
  })
  const busy = change.isPending || !marks.data || !evidence.data?.source_sha256
  const valid =
    Number.isFinite(start) &&
    Number.isFinite(end) &&
    start >= 0 &&
    end >= start &&
    end <= duration
  const edit = (mark: Mark) => {
    setEditing(mark.id)
    setStart(mark.interval.start)
    setEnd(mark.interval.end)
    setLabel(mark.label)
    setPolicy(mark.policy)
    setPinned(mark.pinned)
  }
  return (
    <section className="visual-marks" aria-label="Timeline marks">
      <div className="panel-heading">
        <h3>Marks</h3>
        {canEdit && (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy || marks.data?.revision === 0}
            onClick={() => change.mutate({ action: "undo" })}
          >
            Undo
          </Button>
        )}
      </div>
      {(marks.isError || evidence.isError) && (
        <p role="alert">Marks could not be loaded.</p>
      )}
      <ul>
        {marks.data?.marks
          .slice()
          .sort((a, b) => a.interval.start - b.interval.start)
          .map((mark) => (
            <li key={mark.id}>
              <button
                className="mark-jump"
                onClick={() => seek(mark.interval.start)}
              >
                {stamp(mark.interval.start)}
                {mark.interval.end !== mark.interval.start
                  ? `–${stamp(mark.interval.end)}`
                  : ""}{" "}
                · {mark.label || "Mark"}
                {mark.pinned ? " · Pinned" : ""}
              </button>
              {canEdit && (
                <>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => edit(mark)}
                  >
                    Edit
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() =>
                      change.mutate({ action: "delete", id: mark.id })
                    }
                  >
                    Remove
                  </Button>
                </>
              )}
            </li>
          ))}
      </ul>
      {canEdit && (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            if (valid && !busy) change.mutate({ action: "save" })
          }}
        >
          <div className="mark-times">
            <label>
              Start (s)
              <Input
                type="number"
                min={0}
                max={duration}
                step="0.1"
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
                if (end < time) setEnd(time)
              }}
            >
              Start here
            </Button>
            <label>
              End (s)
              <Input
                type="number"
                min={start}
                max={duration}
                step="0.1"
                value={end}
                onChange={(e) => setEnd(Number(e.target.value))}
              />
            </label>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => {
                setEnd(time)
                if (start > time) setStart(time)
              }}
            >
              End here
            </Button>
          </div>
          <label>
            Label
            <Input
              value={label}
              maxLength={150}
              onChange={(e) => setLabel(e.target.value)}
            />
          </label>
          <div className="mark-actions">
            <ValueSelect
              label="Visual policy"
              value={policy}
              items={[
                { value: "auto", label: "Auto" },
                { value: "still", label: "Readable still" },
                { value: "motion", label: "Motion" },
              ]}
              onChange={(v) => setPolicy(v as Mark["policy"])}
            />
            <label>
              <input
                type="checkbox"
                checked={pinned}
                onChange={(e) => setPinned(e.target.checked)}
              />{" "}
              Pin
            </label>
            <Button type="submit" disabled={busy || !valid}>
              {editing ? "Save mark" : "Add mark"}
            </Button>
            {editing && (
              <Button
                type="button"
                variant="ghost"
                onClick={() => setEditing(null)}
              >
                Cancel
              </Button>
            )}
          </div>
          {change.isError && <p role="alert">{change.error.message}</p>}
        </form>
      )}
    </section>
  )
}
