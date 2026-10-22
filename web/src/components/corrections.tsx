import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { preview, request, type Transcript } from "@/lib/api"

type Passage = Transcript["segments"][number]
import { ValueSelect } from "@/components/value-select"
import type { components } from "@/lib/schema"
type Candidate = components["schemas"]["CorrectionCandidateHit"]
type Correction = components["schemas"]["CorrectionView"]
const timestamp = (value: number) =>
  `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, "0")}`

export function Corrections({
  recordingId,
  segments,
}: {
  recordingId: string
  segments: Passage[]
}) {
  const cache = useQueryClient()
  const [open, setOpen] = useState(false)
  const [segmentId, setSegmentId] = useState("")
  const [phrase, setPhrase] = useState("")
  const [hint, setHint] = useState("")
  const [candidates, setCandidates] = useState<Candidate[] | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [showHistory, setShowHistory] = useState(false)
  const base = `/api/recordings/${recordingId}`
  const history = useQuery({
    queryKey: ["corrections", recordingId],
    enabled: !preview,
    queryFn: () => request<Correction[]>(base + "/corrections"),
    refetchInterval: (query) =>
      query.state.data?.some((c) => ["queued", "running"].includes(c.state))
        ? 2000
        : false,
  })
  const find = useMutation({
    mutationFn: () =>
      request<Candidate[]>(base + "/correction-candidates", {
        method: "POST",
        body: JSON.stringify({ segment_id: segmentId, phrase, hint }),
      }),
    onSuccess: (values) => {
      setCandidates(values)
      setSelected([segmentId])
    },
  })
  const queue = useMutation({
    mutationFn: () =>
      request(base + "/corrections", {
        method: "POST",
        body: JSON.stringify({
          segment_id: segmentId,
          phrase,
          hint,
          segment_ids: selected,
        }),
      }),
    onSuccess: () => {
      setOpen(false)
      setShowHistory(true)
      void cache.invalidateQueries({ queryKey: ["corrections", recordingId] })
    },
  })
  const review = useMutation({
    mutationFn: ({ id, action }: { id: string; action: string }) =>
      request(`${base}/corrections/${id}/${action}`, { method: "POST" }),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["corrections", recordingId] })
      void cache.invalidateQueries({ queryKey: ["transcript", recordingId] })
      void cache.invalidateQueries({ queryKey: ["search"] })
    },
  })
  const resetCandidates = () => {
    setCandidates(null)
    setSelected([])
    find.reset()
    queue.reset()
  }
  const pending =
    history.data?.filter((c) =>
      ["queued", "running", "proposed", "failed"].includes(c.state)
    ) ?? []
  return (
    <div className="corrections">
      <div className="correction-actions">
        <Button
          size="sm"
          variant="ghost"
          disabled={preview || !segments.length}
          onClick={() => {
            const selection = window.getSelection()?.toString().trim() ?? ""
            const passage =
              segments.find((s) => selection && s.text.includes(selection)) ??
              segments[0]
            setSegmentId(passage.id)
            setPhrase(
              selection && passage.text.includes(selection)
                ? selection.slice(0, 150)
                : ""
            )
            setHint("")
            resetCandidates()
            setOpen(true)
          }}
        >
          Correct text
        </Button>
        {!!history.data?.length && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setShowHistory(!showHistory)}
          >
            Corrections{pending.length ? ` (${pending.length})` : ""}
          </Button>
        )}
      </div>
      {history.isError && <p role="alert">Corrections could not be loaded.</p>}
      {showHistory && (
        <div className="correction-history">
          {history.data?.map((c) => (
            <article key={c.id} className="correction-review">
              <small>{c.state}</small>
              <p>{c.original}</p>
              {c.proposal && (
                <strong>{c.proposal.map((s) => s.text).join(" ")}</strong>
              )}
              {c.error && <p role="alert">{c.error}</p>}
              <div>
                {c.state === "proposed" && (
                  <Button
                    size="sm"
                    disabled={review.isPending}
                    onClick={() =>
                      review.mutate({ id: c.id, action: "accept" })
                    }
                  >
                    Accept
                  </Button>
                )}
                {["queued", "running", "proposed", "failed"].includes(
                  c.state
                ) && (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={review.isPending}
                    onClick={() =>
                      review.mutate({ id: c.id, action: "dismiss" })
                    }
                  >
                    Dismiss
                  </Button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
      {review.isError && <p role="alert">{review.error.message}</p>}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          className="correction-dialog"
          aria-describedby={undefined}
        >
          <DialogHeader>
            <DialogTitle>Correct text</DialogTitle>
          </DialogHeader>
          <label>
            Passage
            <ValueSelect
              label="Passage"
              value={segmentId}
              onChange={(id) => {
                setSegmentId(id)
                resetCandidates()
              }}
              className="w-full"
              items={segments.map((s) => ({
                value: s.id,
                label: `${timestamp(s.start)} · ${s.text.slice(0, 90)}`,
              }))}
            />
          </label>
          <p className="correction-source">
            {segments.find((s) => s.id === segmentId)?.text}
          </p>
          <label>
            Misheard text
            <Input
              maxLength={150}
              value={phrase}
              onChange={(e) => {
                setPhrase(e.target.value)
                resetCandidates()
              }}
            />
          </label>
          <label>
            Correct word or hint
            <Input
              maxLength={300}
              value={hint}
              onChange={(e) => {
                setHint(e.target.value)
                resetCandidates()
              }}
            />
          </label>
          <Button
            disabled={!phrase.trim() || !hint.trim() || find.isPending}
            onClick={() => find.mutate()}
          >
            {find.isPending ? "Finding…" : "Find matching passages"}
          </Button>
          {candidates && (
            <>
              <div className="correction-candidates">
                {candidates.map((c) => (
                  <label key={c.id}>
                    <input
                      type="checkbox"
                      checked={selected.includes(c.id)}
                      onChange={(e) =>
                        setSelected(
                          e.target.checked
                            ? [...selected, c.id]
                            : selected.filter((id) => id !== c.id)
                        )
                      }
                    />
                    <span>
                      <small>{timestamp(c.start)}</small> {c.text}
                    </span>
                  </label>
                ))}
              </div>
              <Button
                disabled={
                  !selected.length || selected.length > 10 || queue.isPending
                }
                onClick={() => queue.mutate()}
              >
                {queue.isPending
                  ? "Queuing…"
                  : `Retranscribe selected (${selected.length})`}
              </Button>
            </>
          )}
          {(find.isError || queue.isError) && (
            <p role="alert">{find.error?.message ?? queue.error?.message}</p>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
