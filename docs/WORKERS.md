# Isolated workers

The installer runs a single API process and separate inference containers. Philemon defaults to **one worker**. This change does not expose any port publicly or change account ownership, models, or the existing data-volume identity.

The API retains `/data` (accounts, projects, sources, queue and published artifacts) and the optional read-only embedding model. It does not mount the transcription model or worker workspace. Each worker mounts only its private `/work`, the verified read-only Turbo model, and its own read-only credential mount. It has no account database, application data mount, Docker socket or host process namespace.

The host launcher creates an installation-owned internal Docker bridge. Only the API's browser port is published, on localhost. Worker requests use a separate unadvertised listener on that bridge; it accepts registered worker endpoints only. Internal peers cannot use the browser listener, and the browser listener rejects all worker routes. Forwarded headers cannot select the worker interface. Workers have no external network route and use a fixed API host mapping with external DNS disabled.

Each API start rotates separate worker credentials, stored in dedicated Docker local-driver **tmpfs** volumes with restrictive ownership and permissions. They are not stored in application data, configuration, Docker environment variables or command arguments. Workers reread their own credential before claiming another task. Docker/root access remains trusted; this is not a boundary against the host administrator or a kernel compromise. A compromised worker can access media leased to its identity, so public upload quotas and hostile-media qualification remain release gates.

## Configuration

Existing configurations retain the defaults; no new keys are required.

```toml
[resources]
cpus = 2
memory_gib = 6

[workers]
count = 1
chunk_seconds = 180
# Context on EACH side of a core, where available.
overlap_seconds = 2
api_cpus = 0.5
api_memory_gib = 2
```

`resources` is the aggregate runtime budget. The API reservation is subtracted and the remainder divided evenly among workers. Each worker needs at least 0.5 CPU and 2 GiB RAM; the launcher rejects overcommitted configurations before starting Docker. Container swap is disabled. Model preparation/build resources are separate. More workers duplicate model memory and may be slower when they compete on Philemon's CPU; retain one until a real-host test supports changing it. There is no automatic container spawning or remote-worker enrollment in this slice.

A larger machine could use `count = 2` within an appropriate budget. Stop the installation before changing worker count or resource limits; `start` recreates containers while preserving data and models. Normal Git updates stop the complete owned stack before rebuilding and restart it afterward. No volume deletion or model redownload is required for this change.

## Chunk lifecycle and accuracy

One preparation task extracts normalized audio and visual references and creates approximately three-minute core intervals, preferring nearby silence. Core intervals cover the recording without gaps; each inference chunk includes the configured overlap. Preparation and chunk tasks have durable, identity-bound leases and bounded retries. One worker identity can hold at most one active preparation, chunk or correction task. Extra workers can claim different chunks of the same recording.

Each cut also gets a wider-context boundary task. This costs roughly another 24 seconds of inference per three-minute boundary, in addition to overlap. It favors boundary accuracy over maximum throughput; one worker is not promised a speed improvement. Word timestamps are converted to the recording timeline. Matching normalized words are deduplicated only in shared boundary audio when their temporal intersection exceeds half the shorter word's duration. Repeated words at distinct times and repetition inside a chunk are preserved.

Wider-context output must cover the original boundary words before replacing them. Unresolved omissions fail with a boundary-review error rather than publishing a silently shortened transcript. Retrying preserves completed core results and repeats failed or boundary-review tasks. Final transcript, search index and artifact registration publish in one transaction only after all required tasks reconcile. An expired lease cannot publish late results. Chunk WAVs are validated against the normalized retained PCM audio before tasks become claimable.

Progress shows completed chunks out of the planned total. Preparation, model loading and final publication remain separate stages. Existing SSE keeps the API in one process; additional inference workers do not require additional API replicas.

Visual references still use the current bounded frame extraction. Automatic quality selection, animation-aware retention, pruning, restoration, and exports are separate future work. Originals and private prepared audio remain retained; public hosting still requires aggregate storage reservations, quotas and retention policies.

## Validation boundary

Unit tests cover overlap planning, repetition/duplicate matching, omitted boundary words, stale leases, task ownership, concurrent claims, retry reuse and atomic publication. Container checks verify actual resource/mount/network separation and the packaged inference path. These are local/disposable checks, not a Philemon deployment or a claim of speech accuracy on production recordings. Keep the existing backup and verify a real recording after the operator updates Philemon.
