import type { components } from "./schema"
import { previewRequest } from "./preview"
export type Project = components["schemas"]["Project"]
export type Recording = components["schemas"]["Recording"]
export type Transcript = components["schemas"]["Transcript"]
export type Segment = components["schemas"]["Segment"]
export type SearchHit = components["schemas"]["SearchHit"]
export type Health = {
  worker_configured: boolean
  max_upload_bytes: number
  search: string
}
export const preview = new URLSearchParams(window.location.search).has(
  "preview"
)

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (preview) return previewRequest<T>(path, init)
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  })
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    throw new Error(
      typeof error?.detail === "string"
        ? error.detail
        : `Request failed (${response.status})`
    )
  }
  return response.json()
}

export function uploadSource(
  id: string,
  file: File,
  progress: (value: number) => void,
  signal: AbortSignal
): Promise<Recording> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open("PUT", `/api/recordings/${id}/source`)
    xhr.setRequestHeader("Content-Type", "application/octet-stream")
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable)
        progress(Math.round((event.loaded / event.total) * 100))
    }
    xhr.onload = () => {
      let data
      try {
        data = JSON.parse(xhr.responseText)
      } catch {
        reject(new Error("Invalid upload response"))
        return
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data)
      else
        reject(
          new Error(
            typeof data.detail === "string" ? data.detail : "Upload failed"
          )
        )
    }
    xhr.onerror = () => reject(new Error("Upload connection failed"))
    xhr.onabort = () => reject(new Error("Upload cancelled"))
    signal.addEventListener("abort", () => xhr.abort(), { once: true })
    if (signal.aborted) {
      reject(new Error("Upload cancelled"))
      return
    }
    xhr.send(file)
  })
}
