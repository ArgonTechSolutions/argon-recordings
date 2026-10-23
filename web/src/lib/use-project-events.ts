import { useEffect, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { preview, type Recording } from "@/lib/api"

export function useProjectEvents(projectId: string | undefined) {
  const cache = useQueryClient()
  const [disconnected, setDisconnected] = useState(false)
  useEffect(() => {
    if (!projectId || preview) return
    const key = ["recordings", projectId]
    let source: EventSource | undefined
    let closed = false
    let generation = 0
    const connect = () => {
      source?.close()
      generation++
      if (document.hidden || closed) return
      const current = new EventSource(
        `/api/projects/${encodeURIComponent(projectId)}/events`
      )
      source = current
      current.onopen = () => {
        if (source === current && !closed) setDisconnected(false)
      }
      current.onerror = () => {
        if (source === current && !closed) setDisconnected(true)
      }
      current.addEventListener("access-revoked", () => {
        current.close()
        cache.clear()
        window.dispatchEvent(new Event("recordings-access-changed"))
      })
      current.addEventListener("recordings", (event: MessageEvent<string>) => {
        if (closed || source !== current || document.hidden) return
        const rows = JSON.parse(event.data) as Recording[]
        const received = ++generation
        // A slower initial REST read must not overwrite a newer event snapshot.
        void cache.cancelQueries({ queryKey: key, exact: true }).then(() => {
          if (!closed && received === generation) {
            const previous = cache.getQueryData<Recording[]>(key) ?? []
            cache.setQueryData(key, rows)
            for (const row of rows) {
              const old = previous.find((r) => r.id === row.id)
              if (
                old &&
                JSON.stringify(old.evidence_versions) !==
                  JSON.stringify(row.evidence_versions)
              ) {
                for (const kind of [
                  "transcript",
                  "speakers",
                  "evidence",
                  "marks",
                ])
                  void cache.invalidateQueries({ queryKey: [kind, row.id] })
              }
            }
          }
        })
      })
    }
    connect()
    document.addEventListener("visibilitychange", connect)
    return () => {
      closed = true
      generation++
      source?.close()
      document.removeEventListener("visibilitychange", connect)
    }
  }, [cache, projectId])
  return disconnected
}
