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
      current.addEventListener("recordings", (event: MessageEvent<string>) => {
        if (closed || source !== current || document.hidden) return
        const rows = JSON.parse(event.data) as Recording[]
        const received = ++generation
        // A slower initial REST read must not overwrite a newer event snapshot.
        void cache.cancelQueries({ queryKey: key, exact: true }).then(() => {
          if (!closed && received === generation) cache.setQueryData(key, rows)
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
