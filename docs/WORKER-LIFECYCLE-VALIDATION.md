# Real worker lifecycle qualification

The bounded lifecycle check passed on application commit `da3de95`. It exercises real `run_once` preparation, CPU Turbo inference, chunk publication and FFmpeg extraction through a temporary loopback API. This adds local runtime evidence; the candidate remains **quality blocked**, and P13/P14 are not complete.

## Executed journey

`scripts/check_worker_lifecycle.py` uses the frozen 240-second English PCM input and direct-inference reference from [ASR-REGRESSION.md](ASR-REGRESSION.md), plus the attributed public video from [PUBLIC-VISUAL-EVALUATION.md](PUBLIC-VISUAL-EVALUATION.md). Turbo remains CPU INT8, two threads and one model worker. The test API has one worker slot and one synthetic worker identity.

The ASR recording is uploaded, decoded/prepared by the actual worker and transcribed by real Turbo. The separate visual recording is explicitly seeded as ready with a real FFmpeg reference frame, without claiming speech inference for that fixture.

| Check | Observed result |
| --- | --- |
| Single-worker admission during actual inference | Additional ASR and visual claims return no task |
| Cancel visual work while ASR is active | One queued image task becomes cancelled; the other remains queued |
| Interrupt native ASR process | SIGKILL terminates/reaps the worker in 0.118 seconds; no partial transcript or retained audio is publicly published |
| API and worker restart | Same SQLite/data directory is reopened; existing lease remains effective until natural expiry |
| Reclaim after restart | Same chunk, new lease, after 117.032 seconds; no database clock/lease manipulation |
| Stale lease fencing | Old audio read, heartbeat and completion all return 409 |
| Resume and publish | Only the interrupted chunk has two attempts; the two other chunks have one each; all complete |
| Transcript reference | Published text, times and probabilities match the frozen direct chunk-reconciliation result exactly: 407 words |
| Visual scheduling afterward | Only the uncancelled image publishes, with a valid media checksum |
| Model reference transition | Turbo is loaded after ASR and its model attribute is cleared for the real FFmpeg task |

The process interruption is a crash test. ASR has no public cancellation endpoint in this version; this result must not be described as graceful ASR cancellation or its latency. Queued visual cancellation is tested directly.

Clearing the model reference is not equivalent to returning the process to its initial RSS. Worker resident memory was 1,284,034,560 bytes before the visual task and 1,202,675,712 afterward. The remaining resident heap is material; this single cycle does not establish a long-running memory plateau or the cause of retained allocations. No full-memory-reclamation claim is made.

## Runtime and evidence

API, worker and controller are separate processes inside one disposable validation container. Its actual cgroup values were `cpu.max = 250000 100000`, `memory.max = 6442450944`, `memory.swap.max = 6442450944`. This is a shared aggregate quota; it does not verify installed per-role CPU/memory allocation. The exact inference lock installs with hashes and `pip check` passes. No model weights are downloaded or modified.

The temporary API listens only on container loopback. Every child process is owned and stopped by the harness; temporary database, source copies and crashed-worker scratch are removed afterward. The outer named container also has a 1,200-second wall-clock timeout and ownership-scoped cleanup. No persistent service, production setting, real account or credential is provisioned.

Qualified evidence is ignored under `models/validation/worker-lifecycle/run-02/`: result JSON, worker/API logs, actual claim/progress/completion events and source/model/reference identities. The parent `container.json` binds the result and inference lock to the base image and actual cgroup values. All owned processes and temporary containers were confirmed terminal/absent. Retained evidence occupies about 68 KiB. The first attempt stopped during incomplete visual-fixture setup; it has no passing result and is not counted as runtime qualification.

With the hash-locked inference runtime prepared, use fresh output and an outer bounded runner:

```sh
PYTHONPATH=backend python scripts/check_worker_lifecycle.py \
  --audio models/validation/asr-real/run-01/input-240.wav \
  --video models/validation/public-visual/wikimedia-screencast.webm \
  --model models/turbo --output /evidence/run-03
```

The installed `isolated.worker` daemon loop, production credential rotation, network/filesystem separation between actual API/worker containers, and long-running memory behavior are not qualified by this shared-container test. Word-reference accuracy, failed diarization count quality, representative visual/readability acceptance, private recordings, Philemon and a real owner agent connection remain separate gates. No deployment or installed-host changes occurred.
