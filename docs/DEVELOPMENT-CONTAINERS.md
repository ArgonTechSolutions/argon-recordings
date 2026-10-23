# Temporary development containers

Use the project launcher for bounded Docker checks, rather than unnamed ad hoc runs:

```sh
python3 scripts/dev_container.py --timeout 180 backend-tests -- \
  --cpus=2 --memory=1g --network=none \
  -v "$PWD:/workspace:ro" -w /workspace \
  -e PYTHONPATH=/workspace/backend -e PYTHONDONTWRITEBYTECODE=1 \
  argon-recordings-api:totp \
  python -m pytest -q -p no:cacheprovider backend/tests
```

The image must already be built for that check; the launcher does not build it. Choose a purpose such as `backend-tests`, `browser-check`, `visual-eval` or `diarization-eval`. Names include project, purpose, process and a unique session suffix. Project/session/purpose labels identify ownership without recording credentials.

The launcher uses the current user's rootless socket only and rejects other endpoints and `DOCKER_HOST`. It runs in the foreground with `--rm` and a wall-clock deadline. Success, failure, timeout, Ctrl-C and SIGTERM all trigger cleanup; cleanup verifies the unique session label before stopping/removing that exact container. It never prunes images, build cache, volumes, networks, other sessions or `docker-shared`. Detached/restarting runs and lifecycle/ownership overrides are rejected. SIGKILL, daemon failure or host shutdown can prevent final cleanup; the descriptive name and labels identify the leftover session for a later audit.

The installed application stack already has explicit names and installation/role labels in `scripts/worker_stack.py`. Its operator-controlled persistent lifecycle is separate from this temporary test launcher.

Check the launcher without Docker using `python3 scripts/test_dev_container.py`.
