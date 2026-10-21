import { useEffect, useState } from "react"
import type { Recording } from "@/lib/api"

function duration(seconds: number) {
  const whole = Math.floor(seconds)
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  const remaining = String(whole % 60).padStart(2, "0")
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${remaining}`
    : `${minutes}:${remaining}`
}

export function TranscriptionProgress({ job }: { job: Recording["job"] }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const detail = job?.transcription
  if (!detail || job?.state !== "running") return null
  const age =
    job.updated_at == null ? null : Math.max(0, now / 1000 - job.updated_at)
  const stale = age === null || age > 20
  const elapsed = detail.elapsed_seconds + (age ?? 0)
  const minutes =
    detail.eta_seconds == null ? null : Math.ceil(detail.eta_seconds / 60)
  return (
    <div className="transcription-progress">
      <p>
        Audio covered: {duration(detail.audio_processed_seconds)} /{" "}
        {duration(detail.audio_total_seconds)}
      </p>
      <p>
        {detail.passages} {detail.passages === 1 ? "passage" : "passages"} ·{" "}
        {duration(elapsed)} elapsed
      </p>
      <p role="status" aria-live="polite">
        {stale
          ? age === null
            ? "Waiting for a worker update…"
            : `Last update ${Math.floor(age)} seconds ago · Waiting for worker`
          : minutes === null
            ? "Estimating remaining time…"
            : minutes < 1
              ? "Finishing transcription…"
              : `About ${minutes} min remaining for transcription`}
      </p>
    </div>
  )
}
