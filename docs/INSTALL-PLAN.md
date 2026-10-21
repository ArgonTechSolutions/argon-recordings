# Installation milestone

Build a Python-stdlib launcher with TOML configuration (port, curated embedding model, resource caps), a self-contained Docker image with web/API/Turbo/FFmpeg, explicit pinned model preparation and reuse, and start/stop/status/logs/doctor commands. Preserve local-only access; use an SSH tunnel for the Philemon test. No host packages, daemon configuration, or sudo changes are installed automatically.

Use a non-root runtime, read-only model/root mounts, disk-backed data/work volumes, bounded resources, one supervised transcription worker and loopback API. The owner approved temporary credential generation in container process memory; no secret is persisted in the package/config. Preserve existing records and models; stop never deletes data. Validate config/command construction, actual packaged startup and shutdown, selected-model health, backend contracts, frontend build and a distributable source archive. Live Philemon verification remains owner-run because this dev server cannot reach its SSH alias.

Local implementation and bounded lifecycle verification are complete; actual Philemon acceptance remains owner-run.
