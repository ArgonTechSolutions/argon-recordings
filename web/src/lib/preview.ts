// Synthetic design fixtures. No example below is a processed customer recording.
const projects = [
  {
    id: "preview-product",
    name: "Product walkthroughs",
    created: 1700000000,
    recording_count: 3,
  },
  {
    id: "preview-engineering",
    name: "Engineering notes",
    created: 1700000000,
    recording_count: 0,
  },
]
const passages = [
  [
    0,
    19,
    "Today we are looking at a simpler way to bring a new team into the workspace. The goal is to make the first few minutes feel clear and focused.",
  ],
  [
    19,
    43,
    "Let’s start with the invitation flow. Instead of asking someone to configure everything upfront, we can let them explore a project first.",
  ],
  [
    43,
    67,
    "Here is the important part: permissions should be visible before you send an invitation. A reviewer can browse recordings without changing the source material.",
  ],
  [
    67,
    94,
    "When someone searches for a topic, take them straight to the relevant moment. Keep the transcript and visual reference together so they can check the context.",
  ],
  [
    94,
    125,
    "We should make it equally easy to move back to the full recording. A screenshot is a reference, and the original video is still the best place to inspect motion.",
  ],
  [
    125,
    155,
    "For the next iteration, let’s make the language more direct and move the secondary settings out of the first step. That will give the invitation screen more room to breathe.",
  ],
  [
    155,
    184,
    "To recap: a clear invitation, visible permissions, and a quick way to find the right moment. We can review the updated version together next week.",
  ],
].map(([start, end, text], index) => ({
  id: `preview-segment-${index}`,
  start: Number(start),
  end: Number(end),
  text: String(text),
  words: [],
}))
const recordings = [
  {
    id: "preview-onboarding",
    title: "A clearer onboarding flow",
    duration: 184,
    status: "ready",
  },
  {
    id: "preview-permissions",
    title: "Workspace permissions review",
    duration: 142,
    status: "ready",
  },
  {
    id: "preview-search",
    title: "Search and evidence capture",
    duration: null,
    status: "queued",
  },
].map((r, index) => ({
  ...r,
  project_id: projects[0].id,
  filename: `${r.title}.mp4`,
  language: "en",
  bytes: 12000000,
  sha256: null,
  error: null,
  created: 1700000000 + index,
  artifacts: [],
  job: {
    id: `preview-job-${index}`,
    state: r.status === "ready" ? "completed" : "queued",
    progress: r.status === "ready" ? 1 : 0,
    stage: r.status === "ready" ? "Ready" : "Waiting for worker",
  },
}))
export async function previewRequest<T>(
  path: string,
  init?: RequestInit
): Promise<T> {
  if (init?.method && init.method !== "GET")
    throw new Error("Uploads are unavailable in design preview")
  const url = new URL(path, window.location.origin)
  if (url.pathname === "/api/health")
    return {
      worker_configured: false,
      max_upload_bytes: 268435456,
      search: "keyword",
    } as T
  if (url.pathname === "/api/projects") return projects as T
  if (url.pathname.endsWith("/recordings"))
    return (url.pathname.includes(projects[0].id) ? recordings : []) as T
  if (url.pathname.endsWith("/transcript"))
    return { recording_id: url.pathname.split("/")[3], segments: passages } as T
  if (url.pathname === "/api/search") {
    const words = (url.searchParams.get("q") ?? "")
      .toLowerCase()
      .split(/\s+/)
      .filter(Boolean)
    return passages
      .filter((p) => words.every((w) => p.text.toLowerCase().includes(w)))
      .map((p) => ({
        ...p,
        recording_id: recordings[0].id,
        title: recordings[0].title,
      })) as T
  }
  throw new Error("Unknown preview request")
}
