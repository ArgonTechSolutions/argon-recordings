import { useCallback, useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ArrowDownToLine,
  ArrowLeft,
  ArrowRight,
  AudioLines,
  Check,
  ChevronRight,
  CircleHelp,
  FileVideo,
  Folder,
  Layers3,
  LoaderCircle,
  Menu,
  Moon,
  Pause,
  Play,
  Plus,
  Search,
  ShieldCheck,
  SkipBack,
  SkipForward,
  Sun,
  Upload,
  X,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { useTheme } from "@/components/theme-provider"
import {
  preview,
  request,
  uploadSource,
  type Health,
  type Project,
  type Recording,
  type SearchHit,
  type Transcript,
} from "@/lib/api"
import { Corrections } from "@/components/corrections"
import "./workspace.css"

const stamp = (seconds: number) =>
  `${Math.floor(seconds / 60)
    .toString()
    .padStart(2, "0")}:${Math.floor(seconds % 60)
    .toString()
    .padStart(2, "0")}`
const size = (bytes: number) =>
  bytes < 1024 ** 2
    ? `${Math.round(bytes / 1024)} KB`
    : `${(bytes / 1024 ** 2).toFixed(1)} MB`
const isBusy = (r: Recording) =>
  ["queued", "running", "uploading"].includes(r.status)

function App() {
  const cache = useQueryClient()
  const { theme, setTheme } = useTheme()
  const [projectId, setProjectId] = useState<string>()
  const [recordingId, setRecordingId] = useState<string>()
  const [page, setPage] = useState<"library" | "workspace">(
    preview ? "workspace" : "library"
  )
  const [mobileMenu, setMobileMenu] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  useEffect(() => {
    if (!mobileMenu) return
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileMenu(false)
    }
    document.addEventListener("keydown", close)
    return () => document.removeEventListener("keydown", close)
  }, [mobileMenu])
  const [projectOpen, setProjectOpen] = useState(false)
  const [projectName, setProjectName] = useState("")
  const [searchMode, setSearchMode] = useState("hybrid")
  const [query, setQuery] = useState("")
  const [debounced, setDebounced] = useState("")
  const [time, setTime] = useState(preview ? 43 : 0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [notice, setNotice] = useState("")
  const audio = useRef<HTMLAudioElement>(null)
  const pendingSeek = useRef<number | null>(null)
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => request<Project[]>("/api/projects"),
  })
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => request<Health>("/api/health"),
  })
  const activeProject =
    projects.data?.find((p) => p.id === projectId) ?? projects.data?.[0]
  const recordings = useQuery({
    queryKey: ["recordings", activeProject?.id],
    enabled: !!activeProject,
    queryFn: () =>
      request<Recording[]>(`/api/projects/${activeProject!.id}/recordings`),
    refetchInterval: (query) => (query.state.data?.some(isBusy) ? 2000 : false),
  })
  const current =
    recordings.data?.find((r) => r.id === recordingId) ?? recordings.data?.[0]
  const transcript = useQuery({
    queryKey: ["transcript", current?.id],
    enabled: current?.status === "ready",
    queryFn: () =>
      request<Transcript>(`/api/recordings/${current!.id}/transcript`),
  })
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query), 250)
    return () => clearTimeout(timer)
  }, [query])
  const hits = useQuery({
    queryKey: ["search", activeProject?.id, debounced, searchMode],
    enabled: !!activeProject && !!debounced.trim(),
    queryFn: () =>
      request<SearchHit[]>(
        `/api/search?project_id=${activeProject!.id}&q=${encodeURIComponent(debounced)}&mode=${searchMode}`
      ),
  })
  const createProject = useMutation({
    mutationFn: () =>
      request<Project>("/api/projects", {
        method: "POST",
        body: JSON.stringify({ name: projectName }),
      }),
    onSuccess: async (p) => {
      await cache.invalidateQueries({ queryKey: ["projects"] })
      setProjectId(p.id)
      setProjectOpen(false)
      setProjectName("")
      setPage("library")
    },
  })
  const audioAsset = current?.artifacts?.find((a) => a.kind === "audio")
  const frames = current?.artifacts?.filter((a) => a.kind === "frame") ?? []
  const nearest = frames.reduce<(typeof frames)[number] | undefined>(
    (best, frame) =>
      !best ||
      Math.abs((frame.time ?? 0) - time) < Math.abs((best.time ?? 0) - time)
        ? frame
        : best,
    undefined
  )
  const duration = current?.duration ?? 0
  const segments = transcript.data?.segments ?? []
  const activeSegment = segments.findIndex(
    (s) => time >= s.start && time < s.end
  )
  const chooseRecording = (id: string, at = 0) => {
    setRecordingId(id)
    setPage("workspace")
    setQuery("")
    setTime(at)
    pendingSeek.current = at
    if (current?.id === id && audio.current) audio.current.currentTime = at
  }
  const seek = useCallback(
    (at: number) => {
      const next = Math.max(0, Math.min(duration, at))
      setTime(next)
      if (audio.current) audio.current.currentTime = next
    },
    [duration]
  )
  const download = () => {
    const url = URL.createObjectURL(
      new Blob(
        [segments.map((s) => `[${stamp(s.start)}] ${s.text}`).join("\n\n")],
        { type: "text/plain;charset=utf-8" }
      )
    )
    const a = document.createElement("a")
    a.href = url
    a.download = `${current?.title ?? "recording"}-transcript.txt`
    a.click()
    URL.revokeObjectURL(url)
  }
  const totalReady =
    recordings.data?.filter((r) => r.status === "ready").length ?? 0

  return (
    <div className="app-shell">
      <aside
        id="app-navigation"
        className={`sidebar ${mobileMenu ? "is-open" : ""}`}
      >
        <button
          className="sidebar-close"
          aria-label="Close navigation"
          onClick={() => setMobileMenu(false)}
        >
          <X size={18} />
        </button>
        <a href={preview ? "?preview=1" : "/"} className="brand">
          <span className="brand-mark">
            <Layers3 size={21} />
          </span>
          <span>
            argon<span className="brand-subtitle">RECORDINGS</span>
          </span>
        </a>
        <div className="workspace-label">
          <span className="workspace-avatar">A</span>
          <span>
            Argon workspace
            <small>{preview ? "Design preview" : "Local workspace"}</small>
          </span>
          <ShieldCheck size={15} />
        </div>
        <nav aria-label="Main navigation">
          <button
            className={page === "library" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setPage("library")
              setMobileMenu(false)
            }}
          >
            <Folder size={17} /> Recording library
          </button>
          <button
            className={page === "workspace" ? "nav-item active" : "nav-item"}
            disabled={!current}
            onClick={() => {
              setPage("workspace")
              setMobileMenu(false)
            }}
          >
            <AudioLines size={17} /> Recording workspace
          </button>
        </nav>
        <div className="section-label">
          PROJECTS{" "}
          <button
            aria-label="New project"
            onClick={() => setProjectOpen(true)}
            disabled={preview}
          >
            <Plus size={15} />
          </button>
        </div>
        <nav aria-label="Projects" className="project-nav">
          {projects.data?.map((p) => (
            <button
              key={p.id}
              className={`nav-item ${activeProject?.id === p.id ? "selected-project" : ""}`}
              onClick={() => {
                setProjectId(p.id)
                setRecordingId(undefined)
                setPage("library")
                setQuery("")
                setMobileMenu(false)
              }}
            >
              <span className="project-dot" />
              <span>{p.name}</span>
              <small>{p.recording_count ?? 0}</small>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <button
            className="theme-toggle"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}{" "}
            {theme === "dark" ? "Light appearance" : "Dark appearance"}
          </button>
          <div className="argon-credit">
            An open-source project by <strong>Argon</strong>
          </div>
        </div>
      </aside>
      {mobileMenu && (
        <button
          className="mobile-backdrop"
          aria-label="Dismiss navigation overlay"
          onClick={() => setMobileMenu(false)}
        />
      )}
      <main className="main-shell">
        <header className="topbar">
          <div className="breadcrumbs">
            <button
              className="mobile-menu-button"
              aria-label="Open navigation"
              aria-expanded={mobileMenu}
              aria-controls="app-navigation"
              onClick={() => setMobileMenu(true)}
            >
              <Menu size={20} />
            </button>
            <span>Workspace</span>
            <ChevronRight size={14} />
            <strong>{activeProject?.name ?? "Recording library"}</strong>
          </div>
          <div className="top-actions">
            <span className="local-indicator">
              <span />
              {preview ? "Example content" : "Private workspace"}
            </span>
            <Button
              size="sm"
              onClick={() => setUploadOpen(true)}
              disabled={!activeProject || preview}
            >
              <Plus size={14} /> Upload recording
            </Button>
          </div>
        </header>
        {preview && (
          <div className="preview-banner">
            <span>
              <CircleHelp size={14} /> Design preview · all projects and
              transcript content are synthetic examples.
            </span>
            <a href="/">
              Exit preview <ArrowRight size={13} />
            </a>
          </div>
        )}
        <div className="content-shell">
          <section className="page-heading">
            <div>
              <div className="eyebrow">YOUR WORK, IN CONTEXT</div>
              <h1>
                {page === "workspace" && current
                  ? current.title
                  : (activeProject?.name ?? "Find the moment.")}
              </h1>
              <p>
                {page === "workspace"
                  ? "Read the conversation. Explore the details. Keep the context."
                  : "A home for the conversations and walkthroughs worth coming back to."}
              </p>
            </div>
            <span className="project-counter">
              <Folder size={16} />
              {recordings.data?.length ?? 0} recordings <span> / </span>{" "}
              {totalReady} ready
            </span>
          </section>
          {projects.isError ? (
            <div className="error-state" role="alert">
              <h2>The workspace API is unavailable.</h2>
              <p>
                Check that the local coordinator is running, then try again.
              </p>
              <Button
                variant="outline"
                onClick={() => cache.invalidateQueries()}
              >
                Try again
              </Button>
              <a href="?preview=1">
                Explore the design preview <ArrowRight size={14} />
              </a>
            </div>
          ) : projects.isLoading ? (
            <div className="loading-state">
              <LoaderCircle className="spin" /> Opening your workspace…
            </div>
          ) : !activeProject ? (
            <div className="first-project">
              <div className="empty-icon">
                <Layers3 size={32} />
              </div>
              <div className="eyebrow">
                A LITTLE LESS SEARCHING. A LOT MORE CONTEXT.
              </div>
              <h2>
                Your next useful moment
                <br />
                starts here.
              </h2>
              <p>
                Create a project to collect recordings, browse transcripts,
                <br />
                and return to the part that matters.
              </p>
              <Button onClick={() => setProjectOpen(true)}>
                <Plus size={15} /> Create your first project
              </Button>
              <a href="?preview=1">
                Take a look around first <ArrowRight size={14} />
              </a>
            </div>
          ) : (
            <>
              <div className="library-toolbar">
                <div className="view-tabs">
                  <button
                    className={page === "library" ? "selected" : ""}
                    onClick={() => setPage("library")}
                  >
                    <Folder size={15} /> Library
                  </button>
                  <button
                    className={page === "workspace" ? "selected" : ""}
                    disabled={!current}
                    onClick={() => setPage("workspace")}
                  >
                    <AudioLines size={15} /> Transcript workspace
                  </button>
                </div>
                <div className="search-box">
                  <Search size={16} />
                  <input
                    aria-label="Search project transcripts"
                    placeholder="Search this project…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                  <select
                    aria-label="Search mode"
                    value={searchMode}
                    onChange={(e) => setSearchMode(e.target.value)}
                  >
                    <option value="hybrid">
                      {health.data?.search === "keyword"
                        ? "Keywords"
                        : "All matches"}
                    </option>
                    {health.data?.search !== "keyword" && (
                      <option value="semantic">Meaning</option>
                    )}
                    <option value="keyword">Exact words</option>
                  </select>
                  {query && (
                    <button
                      aria-label="Clear search"
                      onClick={() => setQuery("")}
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              </div>
              {debounced.trim() ? (
                <section className="search-results">
                  <div className="section-label">
                    MATCHING MOMENTS{" "}
                    <span>{hits.data?.length ?? 0} results</span>
                  </div>
                  {hits.isFetching ? (
                    <div className="loading-state">
                      <LoaderCircle className="spin" /> Searching transcripts…
                    </div>
                  ) : hits.isError ? (
                    <p role="alert">
                      {hits.error?.message ?? "Search is unavailable."}
                    </p>
                  ) : hits.data?.length ? (
                    hits.data.map((hit, index) => (
                      <button
                        className="search-hit"
                        key={`${hit.recording_id}-${index}`}
                        onClick={() =>
                          chooseRecording(hit.recording_id, hit.start)
                        }
                      >
                        <span className="time-chip">{stamp(hit.start)}</span>
                        <div>
                          <strong>{hit.title}</strong>
                          <p>{hit.text}</p>
                        </div>
                        <ArrowRight size={18} />
                      </button>
                    ))
                  ) : (
                    <div className="empty-search">
                      <Search size={25} />
                      <h3>No matching passages yet</h3>
                    </div>
                  )}
                </section>
              ) : page === "library" ? (
                <>
                  <div className="recording-grid">
                    {recordings.data?.map((r, i) => (
                      <button
                        className="recording-card"
                        key={r.id}
                        onClick={() => chooseRecording(r.id)}
                      >
                        <div className={`card-preview tone-${i % 3}`}>
                          {r.artifacts?.find((a) => a.kind === "frame") ? (
                            <img
                              src={
                                r.artifacts.find((a) => a.kind === "frame")!.url
                              }
                              alt="Retained visual reference"
                              loading="lazy"
                            />
                          ) : (
                            <>
                              <FileVideo size={36} strokeWidth={1.2} />
                              <span>
                                {preview
                                  ? "Example recording"
                                  : r.status === "ready"
                                    ? "Audio recording"
                                    : "Visual references appear after processing"}
                              </span>
                            </>
                          )}
                          <span className="duration-chip">
                            {r.duration ? stamp(r.duration) : "—"}
                          </span>
                        </div>
                        <div className="card-body">
                          <div className="card-meta">
                            <Badge variant="outline">
                              {r.status === "ready" ? (
                                <Check size={11} />
                              ) : isBusy(r) ? (
                                <LoaderCircle size={11} className="spin" />
                              ) : (
                                <FileVideo size={11} />
                              )}
                              {r.status === "ready"
                                ? "Ready"
                                : r.status === "failed"
                                  ? "Needs attention"
                                  : r.status === "awaiting_upload"
                                    ? "Awaiting upload"
                                    : "Processing queue"}
                            </Badge>
                            <span>{size(r.bytes)}</span>
                          </div>
                          <h3>{r.title}</h3>
                          <div className="card-footer">
                            <span>
                              {r.status === "ready"
                                ? "Open transcript"
                                : (r.job?.stage ?? "Awaiting upload")}
                            </span>
                            <ArrowRight size={16} />
                          </div>
                        </div>
                      </button>
                    ))}
                    <button
                      className="upload-card"
                      onClick={() => setUploadOpen(true)}
                      disabled={preview}
                    >
                      <div className="upload-icon">
                        <Upload size={22} />
                      </div>
                      <h3>Bring another conversation.</h3>
                      <p>
                        Upload a video or audio recording.
                        <br />
                        We’ll help you find your way through it.
                      </p>
                      <span>
                        Add recording <Plus size={14} />
                      </span>
                    </button>
                  </div>
                  {!preview && !health.data?.worker_configured && (
                    <div className="quiet-note">
                      <CircleHelp size={16} />
                      <span>
                        Uploads will wait safely in the queue until a worker is
                        connected.
                      </span>
                    </div>
                  )}
                </>
              ) : current ? (
                <>
                  <div className="workspace-topline">
                    <button onClick={() => setPage("library")}>
                      <ArrowLeft size={15} /> All recordings
                    </button>
                    <label>
                      RECORDING{" "}
                      <select
                        aria-label="Select recording"
                        value={current.id}
                        onChange={(e) => chooseRecording(e.target.value)}
                      >
                        {recordings.data?.map((r) => (
                          <option value={r.id} key={r.id}>
                            {r.title}
                          </option>
                        ))}
                      </select>
                    </label>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!segments.length}
                      onClick={download}
                    >
                      <ArrowDownToLine size={13} /> Download transcript
                    </Button>
                  </div>
                  {current.status !== "ready" ? (
                    <div className="processing-state">
                      <div className="empty-icon">
                        {isBusy(current) ? (
                          <LoaderCircle className="spin" size={30} />
                        ) : (
                          <AudioLines size={30} />
                        )}
                      </div>
                      <h2>
                        {current.status === "failed"
                          ? "This recording needs attention."
                          : current.status === "awaiting_upload"
                            ? "Waiting for the source file."
                            : "Your recording is in good hands."}
                      </h2>
                      <p>
                        {current.error ??
                          current.job?.stage ??
                          "Waiting for upload"}
                      </p>
                      {isBusy(current) && (
                        <progress max={1} value={current.job?.progress ?? 0} />
                      )}
                      <span>
                        The transcript and visual references will appear here
                        when processing finishes.
                      </span>
                    </div>
                  ) : (
                    <div className="recording-workspace">
                      <section
                        className="transcript-panel"
                        aria-label="Transcript"
                      >
                        <div className="panel-heading">
                          <div>
                            <AudioLines size={17} />
                            <h2>Transcript</h2>
                            <span>{segments.length} passages</span>
                          </div>
                          <Badge variant="outline">
                            {current.language === "auto"
                              ? "Detected language"
                              : current.language === "es"
                                ? "Spanish"
                                : "English"}
                          </Badge>
                        </div>
                        <Corrections
                          key={current.id}
                          recordingId={current.id}
                          segments={segments}
                        />
                        <div className="transcript-scroll">
                          {transcript.isLoading ? (
                            <div className="loading-state">
                              Loading transcript…
                            </div>
                          ) : transcript.isError ? (
                            <p role="alert">
                              The transcript could not be loaded.
                            </p>
                          ) : segments.length ? (
                            segments.map((s, i) => (
                              <button
                                key={`${s.start}-${i}`}
                                className={`passage ${activeSegment === i ? "is-current" : ""}`}
                                onClick={() => seek(s.start)}
                              >
                                <span className="passage-time">
                                  {stamp(s.start)}
                                </span>
                                <span>{s.text}</span>
                                {activeSegment === i && (
                                  <span className="passage-marker" />
                                )}
                              </button>
                            ))
                          ) : (
                            <div className="empty-search">
                              <p>No speech was detected in this recording.</p>
                            </div>
                          )}
                        </div>
                      </section>
                      <section
                        className="reference-panel"
                        aria-label="Visual reference"
                      >
                        <div className="panel-heading">
                          <div>
                            <FileVideo size={17} />
                            <h2>Visual reference</h2>
                          </div>
                          <span className="time-chip">
                            {stamp(nearest?.time ?? time)}
                          </span>
                        </div>
                        <div className="visual-stage">
                          {nearest ? (
                            <img
                              src={nearest.url}
                              alt={`Retained frame at ${stamp(nearest.time ?? 0)}`}
                            />
                          ) : preview ? (
                            <ExampleVisual />
                          ) : (
                            <div className="no-visual">
                              <AudioLines size={34} strokeWidth={1} />
                              <h3>Listen to the details.</h3>
                              <p>
                                This recording has no retained visual frames.
                              </p>
                            </div>
                          )}
                        </div>
                        <div className="reference-caption">
                          <span>
                            <span className="reference-dot" />
                            {preview
                              ? "Example visual reference"
                              : nearest
                                ? "Nearest retained frame"
                                : "Audio recording"}
                          </span>
                          <span>
                            {nearest
                              ? `${Math.abs((nearest.time ?? 0) - time).toFixed(1)}s from playhead`
                              : "Source timeline"}
                          </span>
                        </div>
                        <div className="frame-strip">
                          {frames.length ? (
                            frames.slice(0, 20).map((f) => (
                              <button
                                key={f.name}
                                onClick={() => seek(f.time ?? 0)}
                                aria-label={`Go to frame at ${stamp(f.time ?? 0)}`}
                              >
                                <img src={f.url} alt="" loading="lazy" />
                                <span>{stamp(f.time ?? 0)}</span>
                              </button>
                            ))
                          ) : preview ? (
                            <PreviewFrames time={time} onSeek={seek} />
                          ) : (
                            <p>No frames</p>
                          )}
                        </div>
                      </section>
                    </div>
                  )}
                  {current.status === "ready" && (
                    <div className="audio-dock">
                      <div className="audio-label">
                        <div className="audio-icon">
                          <AudioLines size={20} />
                        </div>
                        <div>
                          <strong>{current.title}</strong>
                          <span>
                            {preview
                              ? "Example timeline · no audio attached"
                              : audioAsset
                                ? "Original recording timeline"
                                : "Audio unavailable"}
                          </span>
                        </div>
                      </div>
                      <div className="audio-controls">
                        <button
                          aria-label="Back ten seconds"
                          onClick={() => seek(time - 10)}
                        >
                          <SkipBack size={17} />
                        </button>
                        <Button
                          size="icon-sm"
                          className="play-button"
                          disabled={!audioAsset}
                          aria-label={
                            playing ? "Pause recording" : "Play recording"
                          }
                          onClick={() => {
                            if (audio.current) {
                              if (playing) audio.current.pause()
                              else
                                audio.current
                                  .play()
                                  .catch(() =>
                                    setNotice("Audio playback could not start.")
                                  )
                            }
                          }}
                        >
                          {playing ? <Pause size={16} /> : <Play size={16} />}
                        </Button>
                        <button
                          aria-label="Forward ten seconds"
                          onClick={() => seek(time + 10)}
                        >
                          <SkipForward size={17} />
                        </button>
                        <span>{stamp(time)}</span>
                        <input
                          aria-label="Recording position"
                          type="range"
                          min={0}
                          max={duration || 1}
                          step={0.1}
                          value={Math.min(time, duration)}
                          onChange={(e) => seek(Number(e.target.value))}
                        />
                        <span>{stamp(duration)}</span>
                        <button
                          aria-label="Change playback speed"
                          className="speed-button"
                          onClick={() => {
                            const next = speed >= 2 ? 1 : speed + 0.25
                            setSpeed(next)
                            if (audio.current) audio.current.playbackRate = next
                          }}
                        >
                          {speed}×
                        </button>
                      </div>
                      {audioAsset && (
                        <audio
                          key={current.id}
                          ref={audio}
                          src={audioAsset.url}
                          preload="metadata"
                          onTimeUpdate={(e) =>
                            setTime(e.currentTarget.currentTime)
                          }
                          onPlay={() => setPlaying(true)}
                          onPause={() => setPlaying(false)}
                          onEnded={() => setPlaying(false)}
                          onLoadedMetadata={(e) => {
                            const at = pendingSeek.current ?? 0
                            e.currentTarget.currentTime = at
                            setTime(at)
                            pendingSeek.current = null
                            e.currentTarget.playbackRate = speed
                          }}
                        />
                      )}
                    </div>
                  )}
                </>
              ) : null}
            </>
          )}
          <footer className="page-footer">
            <span>Less scrubbing. More understanding.</span>
            <span>
              ARGON RECORDINGS <span className="footer-dot">·</span> EARLY
              PREVIEW
            </span>
          </footer>
        </div>
      </main>
      <Dialog open={projectOpen} onOpenChange={setProjectOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>A place for your recordings.</DialogTitle>
            <DialogDescription>
              Group related conversations, demos, and walkthroughs in a project.
            </DialogDescription>
          </DialogHeader>
          <form
            className="dialog-form"
            onSubmit={(e) => {
              e.preventDefault()
              createProject.mutate()
            }}
          >
            <label htmlFor="project-name">Project name</label>
            <Input
              id="project-name"
              autoFocus
              maxLength={100}
              placeholder="e.g. Product walkthroughs"
              value={projectName}
              onChange={(e) => setProjectName(e.target.value)}
              required
            />
            {createProject.error && (
              <p className="form-error" role="alert">
                {createProject.error.message}
              </p>
            )}
            <Button
              type="submit"
              disabled={
                createProject.isPending || !projectName.trim() || preview
              }
            >
              {createProject.isPending ? (
                <LoaderCircle className="spin" />
              ) : (
                <Plus size={15} />
              )}{" "}
              Create project
            </Button>
          </form>
        </DialogContent>
      </Dialog>
      {activeProject && (
        <UploadDialog
          open={uploadOpen}
          onOpenChange={setUploadOpen}
          project={activeProject}
          onComplete={(r) => {
            chooseRecording(r.id)
            setUploadOpen(false)
          }}
        />
      )}
      {notice && (
        <div className="notice" role="status">
          {notice}
          <button aria-label="Dismiss message" onClick={() => setNotice("")}>
            <X size={14} />
          </button>
        </div>
      )}
    </div>
  )
}

function UploadDialog({
  open,
  onOpenChange,
  project,
  onComplete,
}: {
  open: boolean
  onOpenChange: (value: boolean) => void
  project: Project
  onComplete: (recording: Recording) => void
}) {
  const cache = useQueryClient()
  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState("")
  const [language, setLanguage] = useState("auto")
  const [urgent, setUrgent] = useState(false)
  const [percent, setPercent] = useState(0)
  const abort = useRef<AbortController | null>(null)
  const draft = useRef<{ id: string; projectId: string } | null>(null)
  const mutation = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("Choose a file first")
      if (file.size > 256 * 1024 ** 2)
        throw new Error("Choose a file smaller than 256 MB")
      abort.current = new AbortController()
      setPercent(0)
      if (!draft.current || draft.current.projectId !== project.id) {
        const r = await request<Recording>(
          `/api/projects/${project.id}/recordings`,
          {
            method: "POST",
            body: JSON.stringify({
              title,
              filename: file.name,
              language,
              priority: urgent ? "urgent" : "normal",
            }),
          }
        )
        draft.current = { id: r.id, projectId: project.id }
      }
      return uploadSource(
        draft.current.id,
        file,
        setPercent,
        abort.current.signal
      )
    },
    onSuccess: async (r) => {
      await Promise.all([
        cache.invalidateQueries({ queryKey: ["recordings"] }),
        cache.invalidateQueries({ queryKey: ["projects"] }),
      ])
      draft.current = null
      setFile(null)
      setTitle("")
      onComplete(r)
    },
  })
  const selectFile = (chosen: File | null) => {
    setFile(chosen)
    setTitle(chosen?.name.replace(/\.[^.]+$/, "") ?? "")
    draft.current = null
    mutation.reset()
  }
  return (
    <Dialog
      open={open}
      onOpenChange={(value) => {
        if (!mutation.isPending) onOpenChange(value)
      }}
    >
      <DialogContent>
        <DialogHeader>
          <div className="dialog-kicker">
            <Upload size={18} /> ADD TO {project.name.toUpperCase()}
          </div>
          <DialogTitle>Bring the conversation.</DialogTitle>
          <DialogDescription>
            Upload a recording. Come back to a transcript and the moments that
            matter.
          </DialogDescription>
        </DialogHeader>
        <form
          className="dialog-form"
          onSubmit={(e) => {
            e.preventDefault()
            mutation.mutate()
          }}
        >
          <label
            className={`file-drop ${file ? "has-file" : ""}`}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault()
              if (!mutation.isPending)
                selectFile(e.dataTransfer.files[0] ?? null)
            }}
          >
            <input
              type="file"
              accept="video/*,audio/*"
              disabled={mutation.isPending}
              onChange={(e) => selectFile(e.target.files?.[0] ?? null)}
            />
            <div className="upload-icon">
              {file ? <FileVideo size={25} /> : <Upload size={25} />}
            </div>
            <strong>{file?.name ?? "Choose a file or drop it here"}</strong>
            <span>
              {file
                ? size(file.size)
                : "Video or audio · up to 5 minutes · 256 MB"}
            </span>
          </label>
          <label htmlFor="recording-title">Recording title</label>
          <Input
            id="recording-title"
            placeholder="Give this conversation a name"
            required
            maxLength={150}
            value={title}
            disabled={mutation.isPending}
            onChange={(e) => setTitle(e.target.value)}
          />
          <div className="upload-options">
            <label>
              Spoken language
              <select
                value={language}
                disabled={mutation.isPending}
                onChange={(e) => setLanguage(e.target.value)}
              >
                <option value="auto">Detect automatically</option>
                <option value="es">Spanish</option>
                <option value="en">English</option>
              </select>
            </label>
            <label className="priority-option">
              <input
                type="checkbox"
                checked={urgent}
                disabled={mutation.isPending}
                onChange={(e) => setUrgent(e.target.checked)}
              />
              <span>
                Prioritize this recording
                <small>Moves ahead of recent normal jobs.</small>
              </span>
            </label>
          </div>
          {mutation.isPending && (
            <div className="upload-progress">
              <progress max={100} value={percent} />
              <span>
                {percent < 100 ? `Uploading ${percent}%` : "Finishing upload…"}
              </span>
            </div>
          )}
          {mutation.error && (
            <p role="alert" className="form-error">
              {mutation.error.message}
            </p>
          )}
          <div className="upload-dialog-footer">
            {mutation.isPending ? (
              <Button
                type="button"
                variant="outline"
                onClick={() => abort.current?.abort()}
              >
                Cancel upload
              </Button>
            ) : (
              <Button
                type="submit"
                disabled={!file || !title.trim() || preview}
              >
                Upload recording <ArrowRight size={14} />
              </Button>
            )}
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function PreviewFrames({
  time,
  onSeek,
}: {
  time: number
  onSeek: (at: number) => void
}) {
  return [0, 30, 60, 90, 120].map((at) => (
    <button
      key={at}
      className={Math.abs(at - time) < 16 ? "selected-frame" : ""}
      onClick={() => onSeek(at)}
      aria-label={`Go to example frame at ${stamp(at)}`}
    >
      <div className="mini-frame">
        <span />
        <span />
        <span />
      </div>
      <span>{stamp(at)}</span>
    </button>
  ))
}

function ExampleVisual() {
  return (
    <div
      className="example-visual"
      aria-label="Synthetic example of a workspace invitation screen"
    >
      <div className="example-browser">
        <span />
        <span />
        <span />
        <small>workspace / invitations</small>
      </div>
      <div className="example-app">
        <aside>
          <span className="tiny-brand">a</span>
          <i />
          <i />
          <i />
          <i />
        </aside>
        <div className="example-form">
          <div className="eyebrow">YOUR TEAM, TOGETHER</div>
          <h3>
            A good place
            <br />
            to start.
          </h3>
          <p>Invite someone into the conversation.</p>
          <div className="fake-input">
            colleague@example.com <span>Reviewer⌄</span>
          </div>
          <div className="fake-invite">
            Send invitation <ArrowRight size={11} />
          </div>
          <div className="example-person">
            <span>JD</span>
            <div>
              Jordan Davis<small>Can view projects and recordings</small>
            </div>
            <Check size={12} />
          </div>
        </div>
      </div>
    </div>
  )
}

export default App
