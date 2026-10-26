"""Bounded, transparent visual-event proposals for retained local recordings.

This module does not extract or delete media. It only returns scored source-time
proposals; accepting, rejecting, pinning, and rendering remain separate actions.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from .visual_runtime import (
    FORMAT_WHITELIST,
    PROTOCOL_WHITELIST,
    RECIPE_VERSION as EXTRACTION_RECIPE_VERSION,
    VisualExtractionError,
    probe_source,
)


ANALYSIS_RECIPE_VERSION = "visual-analysis-v1"
SAMPLE_FPS = 2
ANALYSIS_WIDTH = 256
ANALYSIS_HEIGHT = 144
GRID_COLUMNS = 24
GRID_ROWS = 14
MAX_ANALYSIS_SECONDS = 300
MAX_ANALYSIS_FRAMES = 602
MAX_ANALYSIS_BYTES = ANALYSIS_WIDTH * ANALYSIS_HEIGHT * (MAX_ANALYSIS_FRAMES + 1)
MAX_ANALYSIS_SECONDS_WALL = 45
FALLBACK_SPACING_SECONDS = 60
MAX_FALLBACK_PROPOSALS = 32
MAX_PROPOSALS = 48


def _number(value, name: str) -> float:
    if isinstance(value, bool):
        raise VisualExtractionError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise VisualExtractionError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise VisualExtractionError(f"{name} must be a finite number")
    return result


def _dimensions(source_width: int, source_height: int) -> tuple[int, int]:
    scale = min(1.0, ANALYSIS_WIDTH / source_width, ANALYSIS_HEIGHT / source_height)
    width = max(2, int(source_width * scale) // 2 * 2)
    height = max(2, int(source_height * scale) // 2 * 2)
    return width, height


def _cell_means(frame: bytes, width: int, height: int) -> tuple[int, ...]:
    means = []
    rows, columns = min(GRID_ROWS, height), min(GRID_COLUMNS, width)
    for row in range(rows):
        y0, y1 = row * height // rows, (row + 1) * height // rows
        for column in range(columns):
            x0, x1 = column * width // columns, (column + 1) * width // columns
            total = 0
            count = (x1 - x0) * (y1 - y0)
            for y in range(y0, y1):
                offset = y * width
                total += sum(frame[offset + x0:offset + x1])
            means.append(round(total / count))
    return tuple(means)


def _difference(before: tuple[int, ...], after: tuple[int, ...]) -> dict:
    deltas = tuple(abs(a - b) for a, b in zip(before, after))
    changed = tuple(index for index, value in enumerate(deltas) if value >= 10)
    return {
        "global": sum(deltas) / len(deltas),
        "changed": changed,
        "ratio": len(changed) / len(deltas),
    }


def _similarity_distance(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


def _stable_frame(samples: list[tuple[float, tuple[int, ...]]], start_index: int) -> tuple[int, float]:
    """Find the first sample followed by two nearly unchanged frames."""
    last_start = min(len(samples) - 3, start_index + 6)
    for index in range(max(0, start_index), last_start + 1):
        first = _difference(samples[index][1], samples[index + 1][1])["global"]
        second = _difference(samples[index + 1][1], samples[index + 2][1])["global"]
        if first <= 0.6 and second <= 0.6:
            return index + 1, max(0.0, 1 - (first + second) / 1.2)
    return min(max(0, start_index), len(samples) - 1), 0.0


def _sparse_fallback(start: float, end: float) -> tuple[list[dict], bool]:
    proposals = []
    timestamp = start
    while timestamp < end and len(proposals) < MAX_FALLBACK_PROPOSALS:
        proposals.append({
            "type": "image",
            "source_start": timestamp,
            "source_end": timestamp,
            "timestamp": timestamp,
            "score": 0.0,
            "reason": "Sparse baseline reference when adaptive proposals are unavailable",
            "metrics": {},
            "recipe_version": ANALYSIS_RECIPE_VERSION,
            "extraction_recipe_version": EXTRACTION_RECIPE_VERSION,
            "fallback": True,
        })
        timestamp += FALLBACK_SPACING_SECONDS
    return proposals, timestamp < end


def _proposals(samples: list[tuple[float, tuple[int, ...]]], *, scanned_start: float,
               scanned_end: float) -> tuple[list[dict], dict]:
    if len(samples) < 2:
        return [], {"detected_events": 0, "cursor_suppressed": 0, "deduplicated_static": 0}

    transitions = []
    cursor_suppressed = 0
    for index in range(1, len(samples)):
        diff = _difference(samples[index - 1][1], samples[index][1])
        # Small, short-lived changes are treated as cursor/blink noise. Repeated
        # local edits are recovered below with a rolling spatial union.
        cursor_noise = len(diff["changed"]) <= 3 and diff["global"] < 7
        if cursor_noise:
            cursor_suppressed += 1
        transitions.append({**diff, "time": samples[index][0], "cursor_noise": cursor_noise})

    events = []
    for index, transition in enumerate(transitions):
        changed_count = len(transition["changed"])
        scene_cut = transition["ratio"] >= 0.32 and transition["global"] >= 17
        persistent_index = min(index + 2, len(samples) - 1)
        persistent = _difference(samples[index][1], samples[persistent_index][1])
        persistent_local = len(persistent["changed"]) >= 2 and persistent["global"] >= 0.5
        localized = (changed_count >= 4 and transition["global"] >= 2.0) or persistent_local
        # A series of small code edits can change different cells over a short
        # period even though each individual frame delta is modest.
        rolling = transitions[max(0, index - 5):index + 1]
        active = [item for item in rolling if not item["cursor_noise"]]
        union = set().union(*(set(item["changed"]) for item in active)) if active else set()
        local_sequence = len(union) >= 6 and len(active) >= 3
        if ((not transition["cursor_noise"] or persistent_local)
                and (scene_cut or localized or local_sequence)):
            events.append({
                "index": index + 1,
                "time": transition["time"],
                "metrics": transition if changed_count >= 4 else {**persistent, "time": transition["time"]},
                "event_type": "scene" if scene_cut else "localized-change",
                "rolling_cells": len(union),
            })

    # Collapse repeated detections from one edit/scroll episode to one proposal.
    grouped = []
    for event in events:
        if (grouped and event["event_type"] == grouped[-1]["event_type"]
                and event["time"] - grouped[-1]["time"] <= 5.0):
            previous = grouped[-1]
            if event["metrics"]["global"] > previous["metrics"]["global"]:
                previous["metrics"] = event["metrics"]
                previous["index"] = event["index"]
            previous["time_end"] = event["time"]
            previous["rolling_cells"] = max(previous["rolling_cells"], event["rolling_cells"])
        else:
            grouped.append({**event, "time_end": event["time"]})

    proposals = []
    static_signatures = []
    deduplicated_static = 0
    for event in grouped:
        transition_span = [item for item in transitions
                           if event["time"] - 1.0 <= item["time"] <= event["time_end"] + 0.5
                           and not item["cursor_noise"]]
        sustained_motion = sum(len(item["changed"]) >= 4 and item["global"] >= 2
                               for item in transition_span) >= 3
        score = min(1.0, 0.45 * min(1.0, event["metrics"]["global"] / 32)
                    + 0.35 * min(1.0, event["metrics"]["ratio"] / 0.5)
                    + 0.20 * min(1.0, event["rolling_cells"] / 24))
        frame_index, stability_score = _stable_frame(samples, event["index"])
        image_time, signature = samples[frame_index]
        if sustained_motion:
            kind = "clip"
            source_start = max(scanned_start, event["time"] - 1.0)
            source_end = min(scanned_end, source_start + 30, event.get("time_end", event["time"]) + 2.0)
            image_time = min(source_end, max(source_start, image_time))
            reason = "Sustained screen motion or scrolling; a short clip preserves the change sequence"
        else:
            kind = "image"
            source_start = source_end = image_time
            reason = ("Stable frame after a scene transition" if event["event_type"] == "scene"
                      else "Localized visual change such as a code edit or small on-screen update")
            if (event["event_type"] == "scene"
                    and any(_similarity_distance(signature, old) <= 1.5 for old in static_signatures)):
                deduplicated_static += 1
                continue
            if event["event_type"] == "scene":
                static_signatures.append(signature)
        rounded_start = max(scanned_start, min(scanned_end, round(source_start, 3)))
        rounded_end = max(rounded_start, min(scanned_end, round(source_end, 3)))
        if kind == "image" and rounded_start >= scanned_end:
            rounded_start = rounded_end = source_start
        rounded_time = max(rounded_start, min(rounded_end, round(image_time, 3)))
        proposals.append({
            "type": kind,
            "source_start": rounded_start,
            "source_end": rounded_end,
            "timestamp": rounded_time,
            "score": round(score, 4),
            "reason": reason,
            "metrics": {
                "global_change": round(event["metrics"]["global"], 3),
                "changed_region_ratio": round(event["metrics"]["ratio"], 4),
                "rolling_changed_cells": event["rolling_cells"],
                "event_type": event["event_type"],
                "sustained_motion": sustained_motion,
                "stability_score": round(stability_score, 4),
            },
            "recipe_version": ANALYSIS_RECIPE_VERSION,
            "extraction_recipe_version": EXTRACTION_RECIPE_VERSION,
            "fallback": False,
        })
        if len(proposals) >= MAX_PROPOSALS:
            break
    return proposals, {
        "detected_events": len(grouped),
        "cursor_suppressed": cursor_suppressed,
        "deduplicated_static": deduplicated_static,
    }


def analyze(source: Path, *, start: float = 0.0, end: float | None = None) -> dict:
    """Analyze a bounded local source interval and return reviewable proposals."""
    started = time.monotonic()
    info = probe_source(Path(source))
    requested_start = _number(start, "start")
    requested_end = info.duration if end is None else _number(end, "end")
    if requested_start < 0 or requested_start >= info.duration:
        raise VisualExtractionError("Analysis start is outside the source recording")
    if requested_end <= requested_start or requested_end > info.duration:
        raise VisualExtractionError("Analysis end must be after start and within the source recording")

    scanned_end = min(requested_end, requested_start + MAX_ANALYSIS_SECONDS)
    scan_seconds = scanned_end - requested_start
    width, height = _dimensions(info.display_width, info.display_height)
    frame_size = width * height
    # With copyts, accurate input seeking can apply the container origin twice,
    # and output -t can truncate on the absolute clock. Seek to the preceding
    # keyframe and trim explicitly on retained PTS instead.
    args = [
        "ffmpeg", "-nostdin", "-v", "info", "-threads", "2",
        "-protocol_whitelist", PROTOCOL_WHITELIST, "-format_whitelist", FORMAT_WHITELIST,
        "-copyts", "-noaccurate_seek", "-seek_timestamp", "1",
        "-ss", f"{info.container_start_time + requested_start:.6f}",
        "-i", str(info.path), "-map", "0:v:0", "-an",
        "-vf",
        f"trim=start={info.container_start_time + requested_start:.6f}:"
        f"end={info.container_start_time + scanned_end:.6f},"
        f"fps={SAMPLE_FPS},scale={width}:{height}:flags=area,setsar=1,format=gray,showinfo",
        "-frames:v", str(MAX_ANALYSIS_FRAMES), "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    timed_out = False
    unexpected_termination = False
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            raw, stderr = process.communicate(timeout=MAX_ANALYSIS_SECONDS_WALL)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            raw, stderr = process.communicate()
        if process.returncode not in (0, -9) and not timed_out:
            detail = (stderr or b"").decode("utf-8", "replace")[-800:].strip()
            raise VisualExtractionError(f"FFmpeg visual analysis failed: {detail or 'decode error'}")
        unexpected_termination = process.returncode == -9 and not timed_out
    except OSError as exc:
        raise VisualExtractionError("FFmpeg is required for local visual analysis") from exc

    capped_bytes = len(raw) > MAX_ANALYSIS_BYTES
    raw = raw[:MAX_ANALYSIS_BYTES]
    frame_count = min(len(raw) // frame_size, MAX_ANALYSIS_FRAMES)
    raw = raw[:frame_count * frame_size]
    decoded_pts = [float(value) - info.container_start_time for value in
                   re.findall(rb"\bpts_time:([+-]?(?:\d+(?:\.\d*)?|\.\d+))", stderr)]
    if len(decoded_pts) < frame_count:
        raise VisualExtractionError("FFmpeg did not report source timestamps for decoded analysis frames")
    decoded_pts = decoded_pts[:frame_count]
    samples = []
    for index in range(frame_count):
        timestamp = decoded_pts[index]
        offset = index * frame_size
        if not requested_start <= timestamp < scanned_end:
            continue
        samples.append((timestamp, _cell_means(raw[offset:offset + frame_size], width, height)))

    proposals, counts = _proposals(samples, scanned_start=requested_start, scanned_end=scanned_end)
    partial_reasons = []
    if scanned_end < requested_end:
        partial_reasons.append("duration budget reached")
    if timed_out:
        partial_reasons.append("wall-clock budget reached")
    if unexpected_termination:
        partial_reasons.append("decoder exited before completing the scan")
    if capped_bytes:
        partial_reasons.append("decoded-byte budget reached")
    if not samples:
        partial_reasons.append("no low-resolution frames decoded within interval")
    if samples and samples[-1][0] + 2 / SAMPLE_FPS < scanned_end:
        partial_reasons.append("video frames ended before scan interval")
    if frame_count >= MAX_ANALYSIS_FRAMES and scan_seconds >= MAX_ANALYSIS_SECONDS:
        partial_reasons.append("frame budget reached")
    status = "partial" if partial_reasons else "complete"
    fallback, fallback_truncated = _sparse_fallback(requested_start, requested_end)
    if status != "complete" or not proposals:
        proposals = fallback
        if fallback_truncated:
            partial_reasons.append("sparse fallback proposal cap reached")
            status = "partial"

    decode_complete = bool(samples) and not (timed_out or unexpected_termination or capped_bytes
        or frame_count >= MAX_ANALYSIS_FRAMES or samples[-1][0] + 2 / SAMPLE_FPS < scanned_end)
    actual_scan_end = scanned_end if decode_complete else min(scanned_end,
        samples[-1][0] + 1 / SAMPLE_FPS if samples else requested_start)
    return {
        "status": status,
        "partial_reason": "; ".join(dict.fromkeys(partial_reasons)) or None,
        "analysis_recipe_version": ANALYSIS_RECIPE_VERSION,
        "extraction_recipe_version": EXTRACTION_RECIPE_VERSION,
        "requested_interval": {"start": requested_start, "end": requested_end},
        "scanned_interval": {"start": requested_start, "end": actual_scan_end},
        "decoded_interval": {
            "start": samples[0][0] if samples else None,
            "end": min(scanned_end, samples[-1][0] + 1 / SAMPLE_FPS) if samples else None,
        },
        "proposals": proposals,
        "counts": counts,
        "budgets": {
            "frames_decoded": frame_count,
            "frame_limit": MAX_ANALYSIS_FRAMES,
            "analysis_bytes": len(raw),
            "analysis_byte_limit": MAX_ANALYSIS_BYTES,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "wall_seconds_limit": MAX_ANALYSIS_SECONDS_WALL,
            "analysis_width": width,
            "analysis_height": height,
            "sample_fps": SAMPLE_FPS,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Trusted retained local source file")
    parser.add_argument("--output", type=Path, required=True, help="JSON result path")
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float)
    args = parser.parse_args(argv)
    try:
        result = analyze(args.source, start=args.start, end=args.end)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(f".{args.output.name}.{uuid.uuid4().hex}.partial")
        try:
            temporary.write_text(json.dumps(result, separators=(",", ":")) + "\n")
            os.replace(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (OSError, json.JSONDecodeError, VisualExtractionError) as exc:
        print(json.dumps({"error": str(exc)}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
