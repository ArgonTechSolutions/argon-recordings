# Foundation architecture

The coordinator is the only writer to its local SQLite database. Remote workers use HTTP; never share the SQLite file over a network filesystem.

1. A project owns recordings. Creating a recording reserves metadata and an unclaimable job.
2. The source is streamed to a unique staging file under a byte/disk limit, hashed, fsynced, and moved to its generated source ID. Only then is the job queued. A failed upload is retryable; original client filenames are never storage paths.
3. A worker claims one job under an immediate SQLite transaction. The current implementation permits one valid active lease globally. Lease IDs fence heartbeats, source reads, artifact writes, failure and completion. Leases last 120 seconds; the worker renews them every 20 seconds. Repeated lease loss is bounded to three claims.
4. Source bytes/hash are verified at the worker. FFmpeg probes and bounds decoding; Turbo produces source-relative timestamps. Frames are sought explicitly at 0, 15, 30… seconds, not inferred from a variable-frame-rate proxy.
5. Artifacts are uploaded into a job/lease-specific directory. They are not exposed to the browser until completion validates the manifest and commits transcript, index and artifact references atomically. Completion retries with the same body/lease are idempotent. Stale or changed publications are rejected.
6. The UI queries ready media and transcript routes; original sources are worker-only. SQLite FTS5 is project-scoped and receives quoted terms rather than raw query syntax. This is keyword search, not embeddings or semantic answering.

`recordings.sqlite`, `sources/`, and accepted artifact paths under `results/` are authoritative. `staging/` and unpublished artifact remnants can accumulate after failed jobs; automated garbage collection and quotas beyond upload/disk reserve are not implemented yet. Do not delete them blindly while processing. No automatic original expiry/pruning or schema migration runs. Initial schema creation is for a new application database; unknown versions fail closed.

The first profile is intentionally single-user on loopback. Worker bearer credentials do not authenticate browser users. Uploads, media decoding, model runtime, browser access and worker publication are separate trust boundaries. No Docker socket is mounted in application code, no remote URLs are ingested, and no credential is generated. Public/team deployment needs further authentication, per-project authorization, storage policy, decoder isolation and release qualification.

The first functional tests substitute only inference/extraction where documented; live Chromium checks exercise actual API, SQLite search and file serving with synthetic data. None of these tests qualifies real speech inference through a remote deployment.
