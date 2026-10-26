"""Generated screen-demo fixtures exercise adaptive visual proposals end to end."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from argon_recordings import visual_analysis as analysis


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg analysis fixture requires ffmpeg and ffprobe",
)


WIDTH, HEIGHT = 320, 180


def test_sustained_episode_proposes_a_bounded_clip():
    samples = [(index / 2, (60 if index % 2 else 0,) * 336) for index in range(120)]
    proposals, _ = analysis._proposals(samples, scanned_start=0, scanned_end=60)
    assert proposals and any(p['type'] == 'clip' for p in proposals)
    for proposal in proposals:
        assert 0 <= proposal['source_end']-proposal['source_start'] <= 30
        assert proposal['source_start'] <= proposal['timestamp'] <= proposal['source_end']


@pytest.mark.parametrize('start,end',[(.36,4.137),(.01,.02)])
def test_fractional_scan_matches_publication_contract(screen_demo,start,end):
    from argon_recordings.analysis_jobs import AnalysisResult
    result = analysis.analyze(screen_demo,start=start,end=end)
    AnalysisResult.model_validate(result)
    assert result['requested_interval'] == {'start':start,'end':end}
    assert start <= result['scanned_interval']['end'] <= end
    for proposal in result['proposals']:
        assert start <= proposal['source_start'] <= proposal['source_end'] <= end


def _rect(frame: bytearray, x: int, y: int, width: int, height: int, color: tuple[int, int, int]):
    pixel = bytes(color)
    for row in range(max(0, y), min(HEIGHT, y + height)):
        left, right = max(0, x), min(WIDTH, x + width)
        frame[(row * WIDTH + left) * 3:(row * WIDTH + right) * 3] = pixel * (right - left)


def _screen_frame(index: int) -> bytes:
    frame = bytearray(bytes((10, 12, 16)) * WIDTH * HEIGHT)
    if index < 4:
        return bytes(frame)

    scene_b = 36 <= index < 54
    duplicate_a = index >= 54
    if scene_b:
        _rect(frame, 0, 0, WIDTH, HEIGHT, (224, 226, 232))
        _rect(frame, 26, 22, 266, 18, (32, 65, 112))
        _rect(frame, 26, 58, 168, 8, (78, 91, 112))
        _rect(frame, 26, 78, 238, 8, (105, 117, 136))
        _rect(frame, 26, 98, 210, 8, (105, 117, 136))
        _rect(frame, 26, 129, 80, 25, (63, 144, 112))
        return bytes(frame)

    _rect(frame, 0, 0, WIDTH, HEIGHT, (18, 28, 42))
    _rect(frame, 18, 15, 132, 10, (90, 165, 220))
    shift = min(18, (index - 25) * 2) if 26 <= index < 36 else 0
    for row in range(6):
        y = 43 + row * 19 - shift
        _rect(frame, 22, y, 36 + (row % 3) * 13, 5, (92, 174, 129))
        _rect(frame, 66, y, 70 + (row % 2) * 17, 5, (198, 204, 215))
        _rect(frame, 150, y, 41 + (row % 4) * 11, 5, (215, 173, 98))
    if 12 <= index < 26:
        _rect(frame, 70, 79 - shift, 74, 6, (246, 114, 106))
        _rect(frame, 22, 98 - shift, 118, 6, (92, 174, 129))
    # A small blinking/moving cursor is intentionally not a review event.
    if 4 <= index < 12 and index % 2:
        x = 211 + (index % 3) * 3
        _rect(frame, x, 151, 4, 7, (235, 239, 245))
    return bytes(frame)


def make_demo(path: Path) -> Path:
    return _encode_frames(path, (_screen_frame(index) for index in range(60)))


def _encode_frames(path: Path, frames) -> Path:
    process = subprocess.Popen([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", "2", "-i", "pipe:0",
        "-an", "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", "-y", str(path),
    ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert process.stdin is not None
    for frame in frames:
        process.stdin.write(frame)
    process.stdin.close()
    error = process.stderr.read() if process.stderr else b""
    if process.wait(timeout=30):
        raise AssertionError(error.decode("utf-8", "replace"))
    return path


def _heldout_screen_frame(index: int) -> bytes:
    frame = bytearray(bytes((13, 17, 22)) * WIDTH * HEIGHT)
    if index < 6:
        return bytes(frame)
    slide_b = 48 <= index < 70
    if slide_b:
        _rect(frame, 0, 0, WIDTH, HEIGHT, (230, 222, 208))
        _rect(frame, 38, 24, 190, 14, (98, 54, 86))
        _rect(frame, 38, 61, 230, 7, (110, 108, 104))
        _rect(frame, 38, 80, 204, 7, (110, 108, 104))
        _rect(frame, 38, 99, 178, 7, (110, 108, 104))
        _rect(frame, 208, 124, 76, 36, (208, 127, 63))
        return bytes(frame)

    _rect(frame, 0, 0, WIDTH, HEIGHT, (35, 23, 30))
    _rect(frame, 40, 17, 180, 12, (217, 173, 84))
    shift = min(24, (index - 31) * 3) if 32 <= index < 48 else 0
    for row in range(5):
        y = 48 + row * 21 - shift
        _rect(frame, 45, y, 48 + row * 7, 6, (128, 187, 115))
        _rect(frame, 105, y, 82 + (row % 2) * 12, 6, (212, 204, 187))
        _rect(frame, 198, y, 46 + (row % 3) * 8, 6, (133, 158, 205))
    if 20 <= index < 32:
        _rect(frame, 122, 70 - shift, 96, 8, (241, 104, 83))
        _rect(frame, 45, 95 - shift, 150, 8, (226, 187, 98))
        _rect(frame, 45, 116 - shift, 92, 8, (226, 187, 98))
    if 6 <= index < 20 and index % 3 == 1:
        _rect(frame, 269 + (index % 2) * 4, 152, 3, 8, (244, 234, 190))
    return bytes(frame)


@pytest.fixture
def heldout_demo(tmp_path):
    return _encode_frames(tmp_path / "heldout-screen-demo.mp4",
                          (_heldout_screen_frame(index) for index in range(80)))


@pytest.fixture
def screen_demo(tmp_path):
    return make_demo(tmp_path / "screen-demo.mp4")


def _near(proposals, timestamp, tolerance=1.0):
    return [proposal for proposal in proposals
            if proposal["source_start"] - tolerance <= timestamp <= proposal["source_end"] + tolerance]


def test_proposes_scene_code_scroll_and_slide_events_and_suppresses_cursor(screen_demo):
    result = analysis.analyze(screen_demo)
    proposals = result["proposals"]
    assert result["status"] == "complete"
    assert result["budgets"]["frames_decoded"] <= analysis.MAX_ANALYSIS_FRAMES
    assert result["budgets"]["analysis_bytes"] <= analysis.MAX_ANALYSIS_BYTES
    assert result["budgets"]["elapsed_seconds"] <= analysis.MAX_ANALYSIS_SECONDS_WALL

    designated = {"opening slide": 2.0, "code edit": 6.0, "scroll": 13.0, "demo slide": 18.0}
    found = {name: _near(proposals, timestamp) for name, timestamp in designated.items()}
    assert all(found.values()), {name: [p["source_start"] for p in hits]
                                 for name, hits in found.items()}
    assert any(proposal["type"] == "clip" for proposal in found["scroll"])
    assert all(proposal["score"] > 0 for proposal in proposals)
    assert all(proposal["reason"] and proposal["recipe_version"] == analysis.ANALYSIS_RECIPE_VERSION
               for proposal in proposals)
    assert result["counts"]["cursor_suppressed"] >= 2
    assert result["counts"]["deduplicated_static"] >= 1


def test_budget_exhaustion_returns_partial_sparse_baseline(screen_demo, monkeypatch):
    monkeypatch.setattr(analysis, "MAX_ANALYSIS_SECONDS", 8)
    result = analysis.analyze(screen_demo, end=20)
    assert result["status"] == "partial"
    assert "duration budget reached" in result["partial_reason"]
    assert result["scanned_interval"]["end"] == 8
    assert result["proposals"]
    assert all(proposal["fallback"] for proposal in result["proposals"])
    assert result["proposals"][0]["reason"].startswith("Sparse baseline")


def test_frozen_heldout_sequence_recovers_designated_events(heldout_demo):
    # These event times and visual patterns were held out after calibration.
    result = analysis.analyze(heldout_demo)
    designated = {"opening": 3.0, "code edit": 10.0, "scroll": 16.0, "demo transition": 24.0}
    proposals = result["proposals"]
    matched = {name: _near(proposals, timestamp) for name, timestamp in designated.items()}
    recovered = sum(bool(hits) for hits in matched.values())
    matched_proposals = sum(any(_near([proposal], time) for time in designated.values())
                            for proposal in proposals)
    false_proposals = len(proposals) - matched_proposals
    duplicate_proposals = matched_proposals - recovered
    missed = len(designated) - recovered
    recall = recovered / len(designated)
    print("P08_HELDOUT_METRICS=" + json.dumps({
        "recovered": recovered, "designated": len(designated), "recall": recall,
        "false_proposals": false_proposals, "duplicate_proposals": duplicate_proposals, "missed": missed,
        "proposal_count": len(proposals), "counts": result["counts"],
        "budgets": result["budgets"],
        "matches": {name: [proposal["timestamp"] for proposal in hits]
                    for name, hits in matched.items()},
    }, sort_keys=True))
    assert result["status"] == "complete"
    assert recall >= 0.90, {"matched": {name: [p["timestamp"] for p in hits]
                                          for name, hits in matched.items()},
                           "false_proposals": false_proposals, "missed": missed}
    assert any(p["type"] == "clip" for p in matched["scroll"])
    assert all(p["score"] > 0 and p["reason"] for p in proposals)
    assert result["counts"]["cursor_suppressed"] > 0


def test_rejects_invalid_analysis_window(screen_demo):
    with pytest.raises(analysis.VisualExtractionError, match="within the source"):
        analysis.analyze(screen_demo, start=1, end=10_000)


def test_small_source_uses_nonempty_adaptive_grid(tmp_path):
    tiny = tmp_path / "tiny.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=16x16:rate=2:duration=4", "-an", "-c:v", "libx264",
        "-threads", "2", "-pix_fmt", "yuv420p", "-y", str(tiny),
    ], check=True, timeout=20, capture_output=True)
    result = analysis.analyze(tiny)
    assert result["budgets"]["analysis_width"] == 16
    assert result["budgets"]["analysis_height"] == 16
    assert result["status"] == "complete"


def test_analysis_timestamps_include_delayed_video_track(screen_demo, tmp_path):
    delayed = tmp_path / "delayed.mkv"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-copyts", "-f", "lavfi", "-i",
        "anullsrc=r=48000:cl=stereo:d=8", "-itsoffset", "2", "-i", str(screen_demo),
        "-map", "0:a:0", "-map", "1:v:0", "-c:a", "aac", "-c:v", "copy", "-y", str(delayed),
    ], check=True, timeout=20, capture_output=True)
    result = analysis.analyze(delayed, start=0, end=6)
    assert result["decoded_interval"]["start"] == pytest.approx(2, abs=0.1)
    assert result["decoded_interval"]["end"] >= 5.5


@pytest.mark.parametrize("extension", ["mp4", "mkv"])
def test_shifted_full_scan_has_no_phantom_tail(screen_demo, tmp_path, extension):
    baseline = analysis.analyze(screen_demo)
    shifted = tmp_path / f"shifted.{extension}"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(screen_demo),
        "-c", "copy", "-output_ts_offset", "5", "-y", str(shifted),
    ], check=True, timeout=20, capture_output=True)
    result = analysis.analyze(shifted)
    assert result["requested_interval"] == baseline["requested_interval"]
    assert result["status"] == baseline["status"] == "complete"
    assert result["partial_reason"] is None
    assert result["decoded_interval"] == baseline["decoded_interval"]
    assert result["proposals"] == baseline["proposals"]


def test_cli_emits_stable_json_result(screen_demo, tmp_path):
    output = tmp_path / "analysis.json"
    result = subprocess.run([
        sys.executable, "-m", "argon_recordings.visual_analysis",
        "--source", str(screen_demo), "--output", str(output),
    ], check=True, timeout=60, capture_output=True, text=True,
    env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    stdout = json.loads(result.stdout)
    stored = json.loads(output.read_text())
    assert stored == stdout
    assert stored["analysis_recipe_version"] == "visual-analysis-v1"
