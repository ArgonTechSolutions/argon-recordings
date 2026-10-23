#!/usr/bin/env python3
"""Exercise real TestClient -> diarization worker -> subprocess -> API publication."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from argon_recordings.api import Settings, create_app
from argon_recordings.diarization_worker import run


def main():
    repo = Path(__file__).resolve().parents[2]
    model_root = repo / "models" / "diarization-evaluation"
    data = Path(tempfile.mkdtemp(prefix="api-worker-smoke-", dir=model_root))
    token = "diarization-eval-worker-token"
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app(Settings(data, worker_token=token, reserve=0))
    with TestClient(app, headers=headers) as client:
        project = client.post("/api/projects", json={"name": "Local diarization evaluation"})
        assert project.status_code == 201, project.text
        pid = project.json()["id"]
        recording = client.post(f"/api/projects/{pid}/recordings", json={
            "title": "Public four-speaker smoke sample", "filename": "sample.wav", "language": "en"
        })
        assert recording.status_code == 201, recording.text
        rid = recording.json()["id"]
        assert client.put(f"/api/recordings/{rid}/source", content=b"public fixture reference").status_code == 200
        sample = model_root / "sample.wav"
        with app.state.store.db() as db:
            db.execute("UPDATE recordings SET status='ready',duration=56.8606875 WHERE id=?", (rid,))
            db.execute("UPDATE jobs SET state='completed' WHERE recording_id=?", (rid,))
            db.execute("INSERT INTO segments VALUES(?,?,?,?,?,?)", ("fixture-segment", rid, 0, 1,
                       "Fixture transcript remains unchanged.", "[]"))
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)", (rid, "audio.wav", None, "audio", str(sample)))
        transcript_before = client.get(f"/api/recordings/{rid}/transcript").json()
        evidence = client.get(f"/api/recordings/{rid}/evidence").json()
        request = {"reference": {"recording_id": rid, "source_sha256": evidence["source_sha256"],
                   "versions": evidence["versions"], "interval": {"start": 0, "end": 56.8606875}},
                   "options": {"min_speakers": 1, "max_speakers": 16}}
        queued = client.post(f"/api/recordings/{rid}/diarization", json=request)
        assert queued.status_code == 200, queued.text
        with tempfile.TemporaryDirectory(dir=model_root, prefix="worker-smoke-") as workdir:
            completed = run(client, SimpleNamespace(model="unused"), workdir, directory=model_root)
        assert completed is True
        jobs = client.get(f"/api/recordings/{rid}/diarization").json()
        speakers = client.get(f"/api/recordings/{rid}/speakers").json()
        transcript_after = client.get(f"/api/recordings/{rid}/transcript").json()
        assert jobs[0]["state"] == "completed", jobs
        assert not speakers["stale"] and speakers["turns"], speakers
        assert transcript_after == transcript_before
        output = {"job_state": jobs[0]["state"], "turn_count": len(speakers["turns"]),
                  "speaker_ids": sorted({t["speaker"] for t in speakers["turns"]}),
                  "transcript_unchanged": transcript_after == transcript_before}
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
