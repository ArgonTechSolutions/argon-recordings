# Disposable upgrade qualification

The local old-data upgrade check passes from `3d4eda727a4c9a689899e2be3afa330ebc75a13c` to the application source at `ae64b6e`. The candidate remains **quality blocked**: this compatibility result does not close P13/P14 or approve deployment.

`scripts/check_upgrade.py` exports the baseline from Git into a temporary directory. Three separate Python processes run the old application, the current application against the same disposable data, and a current-source restart. Each enters the real FastAPI lifespan. No installed configuration, Docker volume, user recording or real credential is used. The test terminates its clients and deletes the synthetic database and sessions afterward.

The old application creates four invitation-only accounts, owner/editor/viewer membership, an outsider, an unused invitation, an enrolled synthetic ES256 passkey and recovery records, active app/admin sessions, one completed recording and one queued recording. Worker publication uses explicitly synthetic transcript text and a valid generated silent WAV; no ASR quality is inferred.

The upgrade and restart checks establish:

- All rows and columns from the 34 historical tables survive initial new-source startup unchanged, including session digests, passwords, passkeys, recovery data, invitations, memberships, queued jobs and FTS contents. SQLite integrity and foreign keys pass. After new operations, restart preserves the full updated table snapshot.
- The three retained source/result files preserve SHA-256 hashes. Historical transcript JSON, keyword search and audio retrieval work. Existing app sessions retain their roles; the outsider cannot read the recording and the viewer cannot create marks.
- The historical password and passkey authenticate through the new routes. A real signed WebAuthn fixture assertion authorizes the administrator route.
- New pinned marks, manual unknown-speaker attribution, an expiring agent grant and revocation, and report import/review/export operate on the historical recording. Mark, speaker and reviewed-report state survive restart. Speaker edits do not alter recognized speech. Every ZIP payload matches the manifest checksum and size.
- Old/current installer parsing and generated API/worker arguments remain identical for the same disposable configuration: installation identity, names, volumes, external Turbo mount, selected E5-large profile and one-worker default. With a supplied 2.5-CPU aggregate budget, the generated worker allocation is two CPUs. This compares arguments without executing Docker installation commands or asserting that Philemon has that allocation.

Run this bounded check inside the existing validation image:

```sh
python3 scripts/dev_container.py --timeout 240 upgrade-check -- \
  --cpus=2 --memory=2g --network=none \
  -v "$PWD:/workspace:rw" -w /workspace \
  argon-recordings-validation:local \
  python scripts/check_upgrade.py --evidence models/validation/upgrade-check
```

The ignored result records the baseline/archive hash, current commit, source file hashes, runtime package versions and per-phase checks. The commit identifies the application candidate; file hashes also identify an uncommitted check-script revision. No database/session fixture is retained in that result.

This is source/database compatibility on the validation runtime, not an installed-container upgrade, model inference or proof for another historical schema. It does not verify `/home/argon/argon-recordings`, its real configuration/model files, host CPU allocation, real passkeys, private recordings or a live owner agent connection. Those identities and files remain untouched. Required model-quality, real-footage/readability, real ASR/resource and final release gates remain open.

No reverse-write compatibility claim is made. Old code does not understand new evidence revisions, grants and reports. A future rollback assessment must use a matched source/database backup and retained media; simply launching old code against a database that has received new-feature writes is not qualified by this test. No rollback or production migration is executed here.
