"""Actual local FFmpeg fixture tests for bounded visual extraction."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from argon_recordings import visual_runtime as visual


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg integration fixture requires ffmpeg and ffprobe",
)


def make_video(path: Path, *, vf_source: str = "testsrc2=size=320x240:rate=10:duration=4",
               filters: str | None = None, rotate: bool = False, aspect: str | None = None) -> Path:
    target = path.with_suffix(".base.mp4") if rotate else path
    command = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", vf_source,
               "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if filters:
        command += ["-vf", filters, "-fps_mode", "vfr"]
    if aspect:
        command += ["-aspect", aspect]
    command += ["-y", str(target)]
    subprocess.run(command, check=True, timeout=45, capture_output=True)
    if rotate:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(target), "-c", "copy",
                        "-metadata:s:v:0", "rotate=90", "-y", str(path)],
                       check=True, timeout=15, capture_output=True)
    return path


@pytest.fixture
def cfr_video(tmp_path):
    return make_video(tmp_path / "cfr.mp4")


def test_still_uses_next_actual_source_pts_and_never_upscales(cfr_video, tmp_path):
    result = visual.extract(cfr_video, tmp_path / "still", {"kind": "image", "start": 0.36})
    assert result["filename"] == "image.jpg"
    assert result["kind"] == "image"
    assert result["requested_start"] == pytest.approx(0.36)
    assert result["actual_start"] == pytest.approx(0.4, abs=0.02)
    assert result["actual_end"] == result["actual_start"]
    assert result["fps"] is None
    assert result["width"] <= result["source_width"]
    assert result["height"] <= result["source_height"]
    assert len(result["sha256"]) == 64
    assert result["bytes"] == (tmp_path / "still/image.jpg").stat().st_size


def test_rotated_source_uses_display_space_for_crop(cfr_video, tmp_path):
    rotated = make_video(tmp_path / "rotated.mp4", rotate=True, aspect="16:9")
    info = visual.probe_source(rotated)
    assert info.rotation_degrees == 90
    assert (info.display_width, info.display_height) == (240, 320)
    assert info.sample_aspect_ratio == "4:3"
    assert info.display_aspect_ratio == pytest.approx(9 / 16)
    result = visual.extract(rotated, tmp_path / "rotated-still", {
        "kind": "image", "start": 0.2,
        "crop": {"x": 0, "y": 0, "width": 0.5, "height": 0.5},
    })
    assert result["source_rotation_degrees"] == 90
    assert result["rotation_applied"] is True
    assert (result["width"], result["height"]) == (90, 160)
    assert result["output_sample_aspect_ratio"] == "1:1"
    assert result["output_display_aspect_ratio"] == pytest.approx(9 / 16, abs=0.01)
    assert result["width"] <= 120 and result["height"] <= 160
    clip = visual.extract(rotated, tmp_path / "rotated-clip", {
        "kind": "clip", "start": 0.2, "end": 0.8, "fps": 10,
        "crop": {"x": 0, "y": 0, "width": 0.5, "height": 0.5},
    })
    assert (clip["width"], clip["height"]) == (90, 160)
    assert clip["output_sample_aspect_ratio"] == "1:1"
    assert clip["output_display_aspect_ratio"] == pytest.approx(9 / 16, abs=0.01)


def test_normalizes_container_origin_and_delayed_video_track(cfr_video, tmp_path):
    shifted = tmp_path / "shifted.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(cfr_video), "-map", "0:v:0",
                    "-c", "copy", "-output_ts_offset", "5", "-y", str(shifted)],
                   check=True, timeout=15, capture_output=True)
    shifted_info = visual.probe_source(shifted)
    assert shifted_info.container_start_time == pytest.approx(5, abs=0.05)
    shifted_result = visual.extract(shifted, tmp_path / "shifted-still", {"kind": "image", "start": 0.36})
    assert shifted_result["actual_start"] == pytest.approx(0.4, abs=0.03)

    delayed = tmp_path / "delayed.mkv"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-copyts", "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=stereo:d=6", "-itsoffset", "2", "-i", str(cfr_video),
                    "-map", "0:a:0", "-map", "1:v:0", "-c:a", "aac", "-c:v", "copy", "-y", str(delayed)],
                   check=True, timeout=15, capture_output=True)
    delayed_info = visual.probe_source(delayed)
    assert delayed_info.video_start_time - delayed_info.container_start_time == pytest.approx(2, abs=0.05)
    delayed_result = visual.extract(delayed, tmp_path / "delayed-still", {"kind": "image", "start": 2.36})
    assert delayed_result["actual_start"] == pytest.approx(2.4, abs=0.05)


def test_vfr_clip_samples_without_frame_duplication_and_is_muted(cfr_video, tmp_path):
    vfr = make_video(tmp_path / "vfr.mp4", filters="select='not(mod(n,3))'")
    info = visual.probe_source(vfr)
    assert info.variable_frame_rate
    result = visual.extract(vfr, tmp_path / "clip", {
        "kind": "clip", "start": 0.2, "end": 2.4, "fps": 10,
    })
    assert result["filename"] == "clip.mp4"
    assert result["source_variable_frame_rate"] is True
    assert result["actual_start"] >= 0.2
    assert result["actual_end"] <= 2.4
    assert 1 <= result["output_frame_count"] <= 22
    assert result["requested_fps"] == 10
    assert result["sampling_fps_ceiling"] == pytest.approx(info.nominal_frame_rate)
    assert result["fps"] <= result["sampling_fps_ceiling"] + 0.01
    metadata = visual._probe_json(["-show_streams", str(tmp_path / "clip/clip.mp4")], timeout=5)
    assert not any(stream.get("codec_type") == "audio" for stream in metadata["streams"])
    frame_data = visual._probe_json(["-select_streams", "v:0", "-show_frames",
                                     "-show_entries", "frame=best_effort_timestamp_time",
                                     str(tmp_path / "clip/clip.mp4")], timeout=5)
    pts = [float(frame["best_effort_timestamp_time"]) for frame in frame_data["frames"]]
    assert all(b > a for a, b in zip(pts, pts[1:]))
    assert all(b - a >= 1 / result["sampling_fps_ceiling"] - 0.01 for a, b in zip(pts, pts[1:]))


def test_rejects_corrupt_source(cfr_video, tmp_path):
    corrupt = tmp_path / "broken.mp4"
    corrupt.write_bytes(b"not an mp4")
    with pytest.raises(visual.VisualExtractionError, match="corrupt|metadata"):
        visual.extract(corrupt, tmp_path / "out", {"kind": "image", "start": 0})


def test_rejects_playlist_demuxers(cfr_video, tmp_path):
    # Playlists can point FFmpeg at unrelated local paths or network protocols.
    playlist = tmp_path / "untrusted.ffconcat"
    playlist.write_text(f"ffconcat version 1.0\nfile '{cfr_video}'\n")
    with pytest.raises(visual.VisualExtractionError):
        visual.probe_source(playlist)


@pytest.mark.parametrize("recipe, message", [
    ({"kind": "clip", "start": 0, "end": 30.01}, "30 seconds"),
    ({"kind": "clip", "start": 0, "end": 1, "fps": 31}, "frame sampling"),
    ({"kind": "image", "start": 0, "max_width": 3841}, "max_width"),
    ({"kind": "image", "start": 0, "crop": {"x": 0.8, "y": 0, "width": 0.3, "height": 1}}, "within the frame"),
])
def test_rejects_invalid_bounds_before_decode(cfr_video, tmp_path, recipe, message):
    with pytest.raises(visual.VisualExtractionError, match=message):
        visual.extract(cfr_video, tmp_path / "out", recipe)


def test_rejects_time_outside_source(cfr_video, tmp_path):
    with pytest.raises(visual.VisualExtractionError, match="outside the source"):
        visual.extract(cfr_video, tmp_path / "out", {"kind": "image", "start": 10})


def test_source_resolution_still_obeys_source_pixel_size(cfr_video, tmp_path):
    result = visual.extract(cfr_video, tmp_path / "native", {
        "kind": "image", "start": 0.3, "source_resolution": True,
    })
    assert result["width"] == result["source_width"]
    assert result["height"] == result["source_height"]


def test_source_resolution_preserves_width_above_default_limit(tmp_path):
    high_resolution = make_video(
        tmp_path / "wide.mp4", vf_source="testsrc2=size=2048x128:rate=2:duration=2"
    )
    result = visual.extract(high_resolution, tmp_path / "wide-still", {
        "kind": "image", "start": 0.5, "source_resolution": True,
    })
    assert result["source_width"] == 2048
    assert result["width"] == 2048
    assert result["width"] > 1920
    assert result["width"] <= result["source_width"]


def test_source_byte_limit_is_enforced(cfr_video, tmp_path, monkeypatch):
    monkeypatch.setattr(visual, "MAX_SOURCE_BYTES", 1)
    with pytest.raises(visual.VisualExtractionError, match="input-size limit"):
        visual.probe_source(cfr_video)


def test_decoded_pixel_budget_is_enforced(cfr_video, tmp_path, monkeypatch):
    monkeypatch.setattr(visual, "MAX_DECODED_PIXELS", 100)
    with pytest.raises(visual.VisualExtractionError, match="decoded-pixel budget"):
        visual.extract(cfr_video, tmp_path / "out", {"kind": "clip", "start": 0.2, "end": 1.2, "fps": 10})


def test_cli_writes_versioned_worker_manifest(cfr_video, tmp_path):
    recipe = tmp_path / "recipe.json"
    manifest = tmp_path / "manifest.json"
    recipe.write_text(json.dumps({"kind": "image", "start": 0.4, "end": 0.4}))
    result = subprocess.run([
        sys.executable, "-m", "argon_recordings.visual_runtime",
        "--source", str(cfr_video), "--output", str(tmp_path / "cli-output"),
        "--recipe", str(recipe), "--manifest", str(manifest),
    ], check=True, timeout=20, capture_output=True, text=True, env={
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    })
    payload = json.loads(result.stdout)
    stored = json.loads(manifest.read_text())
    assert stored == payload
    assert stored["filename"] == "image.jpg"
    assert stored["recipe_version"] == "visual-v1"
    assert stored["actual_start"] == pytest.approx(0.4, abs=0.02)
    assert stored["source_display_aspect_ratio"] == pytest.approx(4 / 3)
    assert stored["source_origin_time"] == pytest.approx(0)
    assert stored["source_video_start_time"] == pytest.approx(0)
    assert stored["rotation"] == 0
    assert stored["output_sample_aspect_ratio"] == "1:1"
    assert stored["output_display_aspect_ratio"] == pytest.approx(4 / 3)
    assert stored["requested_fps"] is None
    assert stored["sampling_fps_ceiling"] is None
