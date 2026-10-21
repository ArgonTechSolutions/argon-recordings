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
    const timer = setInterval(() => setNow(Date.now()), 5000)
    return () => clearInterval(timer)
  }, [])
  const detail = job?.transcription
  if (!detail || job?.state !== "running") return null
  const stale = !job.updated_at || now / 1000 - job.updated_at > 20
  const minutes =
    detail.eta_seconds == null ? null : Math.ceil(detail.eta_seconds / 60)
  return (
    <div className="transcription-progress" role="status" aria-live="polite">
      <p>
        Audio covered: {duration(detail.audio_processed_seconds)} /{" "}
        {duration(detail.audio_total_seconds)}
      </p>
      <p>
        {detail.passages} {detail.passages === 1 ? "passage" : "passages"} ·{" "}
        {duration(detail.elapsed_seconds)} elapsed
      </p>
      <p>
        {stale
          ? "Waiting for a worker update…"
          : minutes === null
            ? "Estimating remaining time…"
            : minutes < 1
              ? "Finishing transcription…"
              : `About ${minutes} min remaining for transcription`}
      </p>
    </div>
  )
}
