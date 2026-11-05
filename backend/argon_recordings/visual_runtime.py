"""Bounded local FFmpeg extraction for retained recording evidence.

The caller must resolve recording ownership and supply a trusted retained source
path or an internal leased loopback capability. Arbitrary remote URLs and
caller-selected output filenames are rejected.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import os
import subprocess
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


RECIPE_VERSION = "visual-v1"
WORKER_MANIFEST_FIELDS = frozenset({
    "filename", "kind", "requested_start", "requested_end", "actual_start", "actual_end",
    "width", "height", "fps", "source_width", "source_height", "source_fps", "rotation",
    "output_sample_aspect_ratio", "output_display_aspect_ratio", "requested_fps", "sampling_fps_ceiling",
    "output_frame_count", "source_avg_frame_rate", "source_nominal_frame_rate",
    "source_variable_frame_rate", "source_rotation_degrees", "rotation_applied",
    "source_origin_time", "source_video_start_time",
    "source_sample_aspect_ratio", "source_display_sample_aspect_ratio", "source_display_aspect_ratio",
    "source_coded_width", "source_coded_height", "source_pts_start", "source_pts_end",
    "crop", "sha256", "bytes", "recipe_version",
})
PROTOCOL_WHITELIST = "file,pipe"
FORMAT_WHITELIST = "asf,avi,flv,image2,image2pipe,jpeg_pipe,matroska,mjpeg,mov,mpeg,mpegts,ogg"
MAX_CLIP_SECONDS = 30.0
MAX_FPS = 30.0
MAX_WIDTH = 3840
MAX_SOURCE_BYTES = 8 * 1024**3
MAX_SOURCE_FRAME_PIXELS = 16_777_216
MAX_DECODED_PIXELS = 1_500_000_000
MAX_PROBE_FRAMES = 5000
MAX_PREFIX_DECODE_SECONDS = 120.0
MAX_IMAGE_BYTES = 20 * 1024**2
MAX_CLIP_BYTES = 64 * 1024**2
PROBE_TIMEOUT_SECONDS = 15
EXTRACTION_TIMEOUT_SECONDS = 120


class VisualExtractionError(ValueError):
    """A safe, caller-presentable validation or bounded extraction failure."""


class _NoSourceFrames(VisualExtractionError):
    """A demuxer seek may have skipped the preceding decoding keyframe."""


@dataclass(frozen=True)
class Crop:
    """Normalized crop rectangle in the rotated display coordinate space."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class SourceInfo:
    path: Path
    duration: float
    container_start_time: float
    video_start_time: float
    coded_width: int
    coded_height: int
    display_width: int
    display_height: int
    rotation_degrees: int
    sample_aspect_ratio: str
    display_sample_aspect_ratio: float
    display_aspect_ratio: float
    avg_frame_rate: float
    nominal_frame_rate: float
    variable_frame_rate: bool
    stream_index: int
    size_bytes: int


def _finite_number(value, label: str) -> float:
    if isinstance(value, bool):
        raise VisualExtractionError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise VisualExtractionError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise VisualExtractionError(f"{label} must be a finite number")
    return number


def _rate(value) -> float:
    try:
        rate = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError, TypeError):
        return 0.0
    return rate if math.isfinite(rate) and rate > 0 else 0.0


def network_guard(arguments):
    urls=[str(value) for value in arguments if str(value).startswith('http:')]
    if not urls:return arguments,None
    import secrets,urllib.request
    url=urls[0];source_protocols(url)
    if len(urls)!=1:raise VisualExtractionError('One leased source is required')
    guard=Path(os.environ.get('RECORDINGS_VISUAL_NET_GUARD','/app/lib/visual_netguard.so'))
    if not guard.is_file():raise VisualExtractionError('Seekable source network fence is unavailable')
    control=os.environ.get('RECORDINGS_VISUAL_SOURCE_CONTROL','')
    nonce=secrets.token_hex(24);proof=secrets.token_hex(24)
    request=urllib.request.Request(url+'/r',data=json.dumps({'nonce':nonce,'proof':proof}).encode(),headers={'X-Guard-Control':control},method='POST')
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as response:
        if response.status!=204:raise VisualExtractionError('Could not prepare source network fence')
    from urllib.parse import urlsplit
    parsed=urlsplit(url)
    env=dict(os.environ)
    for key in list(env):
        if key.lower() in {'http_proxy','https_proxy','all_proxy','no_proxy'} or key in {'LD_AUDIT','RECORDINGS_VISUAL_SOURCE_CONTROL'}:env.pop(key,None)
    env.update(LD_PRELOAD=str(guard),RECORDINGS_VISUAL_PROXY_PORT=str(parsed.port),RECORDINGS_VISUAL_ATTEST_PATH=parsed.path+'/a/'+nonce+'/'+proof)
    return [url+'/'+nonce if value==url else value for value in arguments],env


def _run(arguments: list[str], *, timeout: float, capture: bool = False) -> subprocess.CompletedProcess:
    arguments,environment=network_guard(arguments)
    try:
        return subprocess.run(
            arguments,
            env=environment,
            check=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired as exc:
        raise VisualExtractionError("Media processing exceeded its time limit; shorten the range or use a smaller image") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace")[-1200:].strip()
        if "No such file" in detail or "Invalid data" in detail or "moov atom not found" in detail:
            raise VisualExtractionError("Source video is corrupt or has no readable video stream") from exc
        raise VisualExtractionError(f"FFmpeg could not extract this media: {detail or 'decode failed'}") from exc
    except OSError as exc:
        raise VisualExtractionError("FFmpeg and ffprobe must be installed in the worker runtime") from exc


@dataclass(frozen=True)
class LeasedSource:
    url: str
    size: int


def source_protocols(value):
    value=str(value)
    if value.startswith('http:'):
        if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{4,5}/[a-f0-9]{48}',value):
            raise VisualExtractionError('Only the active loopback source capability is accepted')
        return 'file,pipe,http,tcp'
    return PROTOCOL_WHITELIST


def _probe_json(arguments: list[str], *, timeout: float) -> dict:
    result = _run(["ffprobe", "-v", "error", "-protocol_whitelist", source_protocols(arguments[-1]),
                   "-format_whitelist", FORMAT_WHITELIST, "-max_alloc", "67108864", "-of", "json", *arguments],
                  timeout=timeout, capture=True)
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise VisualExtractionError("Could not read video metadata") from exc


def probe_source(source: Path) -> SourceInfo:
    """Inspect the retained source and reject malformed or expensive media."""
    if isinstance(source,LeasedSource):
        source_protocols(source.url)
        if not 0<source.size<=MAX_SOURCE_BYTES:raise VisualExtractionError('Invalid source size')
        resolved=source.url;size_bytes=source.size
    else:
        path = Path(source)
        if path.is_symlink():
            raise VisualExtractionError("Source must be a retained regular file")
        try:
            resolved = path.resolve(strict=True)
            stat = resolved.stat()
        except OSError as exc:
            raise VisualExtractionError("Retained video source is unavailable") from exc
        if not resolved.is_file() or stat.st_size <= 0:
            raise VisualExtractionError("Retained video source is unavailable")
        if stat.st_size > MAX_SOURCE_BYTES:
            raise VisualExtractionError("Source video exceeds the worker input-size limit")
        size_bytes=stat.st_size

    metadata = _probe_json(
        ["-show_format", "-show_streams", str(resolved)], timeout=PROBE_TIMEOUT_SECONDS
    )
    streams = [s for s in metadata.get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        raise VisualExtractionError("Source recording has no video stream")
    stream = streams[0]
    try:
        width, height = int(stream["width"]), int(stream["height"])
        duration = _finite_number(metadata.get("format", {}).get("duration"), "Source duration")
    except (KeyError, TypeError, ValueError) as exc:
        raise VisualExtractionError("Video metadata is incomplete") from exc
    if width <= 0 or height <= 0 or duration <= 0:
        raise VisualExtractionError("Source video dimensions and duration must be positive")
    container_start = metadata.get("format", {}).get("start_time")
    if container_start is None:
        stream_origins = [_finite_number(item.get("start_time"), "Stream start time")
                          for item in metadata.get("streams", []) if item.get("start_time") is not None]
        container_start_time = min(stream_origins, default=0.0)
    else:
        container_start_time = _finite_number(container_start, "Container start time")
    video_start_time = _finite_number(stream.get("start_time", container_start_time), "Video start time")
    # Container duration is not uniformly a span: MOV edit lists and Matroska
    # can include a nonzero timestamp origin. Prefer actual A/V stream ends
    # when every timed stream supplies them, preserving delayed tracks/tails.
    timed_streams = [item for item in metadata.get("streams", [])
                     if item.get("codec_type") in ("audio", "video")
                     and not item.get("disposition", {}).get("attached_pic")]
    if timed_streams and all(item.get("duration") is not None for item in timed_streams):
        duration = max(
            _finite_number(item.get("start_time", container_start_time), "Stream start time")
            + _finite_number(item["duration"], "Stream duration")
            for item in timed_streams
        ) - container_start_time
    elif set(metadata.get("format", {}).get("format_name", "").split(",")) & {"matroska", "webm"}:
        # Matroska Segment Duration is the end timestamp on the segment clock;
        # FFmpeg's muxer writes max(packet timestamp + packet duration).
        duration -= container_start_time
    if not math.isfinite(duration) or duration <= 0:
        raise VisualExtractionError("Source timeline duration must be positive")
    if width * height > MAX_SOURCE_FRAME_PIXELS:
        raise VisualExtractionError("Source frame exceeds the decoded-pixel limit")

    rotation = 0
    for side_data in stream.get("side_data_list", []):
        raw_rotation = side_data.get("rotation")
        if raw_rotation is not None:
            rotation = int(round(_finite_number(raw_rotation, "Video rotation"))) % 360
            break
    rotation = rotation if rotation in (0, 90, 180, 270) else 0
    display_width, display_height = (height, width) if rotation in (90, 270) else (width, height)
    sar = stream.get("sample_aspect_ratio") or "1:1"
    sar_value = _rate(sar.replace(":", "/")) or 1.0
    display_sar = 1 / sar_value if rotation in (90, 270) else sar_value
    dar = display_width * display_sar / display_height
    avg_fps = _rate(stream.get("avg_frame_rate"))
    nominal_fps = _rate(stream.get("r_frame_rate"))
    vfr = bool(avg_fps and nominal_fps and abs(avg_fps - nominal_fps) > max(0.05, nominal_fps * 0.01))
    return SourceInfo(
        path=resolved,
        duration=duration,
        container_start_time=container_start_time,
        video_start_time=video_start_time,
        coded_width=width,
        coded_height=height,
        display_width=display_width,
        display_height=display_height,
        rotation_degrees=rotation,
        sample_aspect_ratio=sar,
        display_sample_aspect_ratio=display_sar,
        display_aspect_ratio=dar,
        avg_frame_rate=avg_fps,
        nominal_frame_rate=nominal_fps,
        variable_frame_rate=vfr,
        stream_index=int(stream.get("index", 0)),
        size_bytes=size_bytes,
    )


def _validate_recipe(recipe: dict) -> dict:
    if not isinstance(recipe, dict):
        raise VisualExtractionError("Extraction recipe must be an object")
    kind = recipe.get("kind")
    if kind not in {"image", "clip"}:
        raise VisualExtractionError("kind must be image or clip")
    start = _finite_number(recipe.get("start"), "start")
    end = recipe.get("end")
    if kind == "image":
        if end is not None and _finite_number(end, "end") != start:
            raise VisualExtractionError("Still extraction accepts a point timestamp only")
        end = start
    else:
        end = _finite_number(end, "end")
        if end <= start:
            raise VisualExtractionError("Clip end must be after its start")
        if end - start > MAX_CLIP_SECONDS:
            raise VisualExtractionError("Video clips are limited to 30 seconds; shorten the requested range")
    max_width = recipe.get("max_width", 1920)
    if isinstance(max_width, bool) or not isinstance(max_width, int) or not 64 <= max_width <= MAX_WIDTH:
        raise VisualExtractionError(f"max_width must be an integer from 64 to {MAX_WIDTH}")
    source_resolution = recipe.get("source_resolution", False)
    if not isinstance(source_resolution, bool) or (source_resolution and kind != "image"):
        raise VisualExtractionError("source_resolution is available for still images only")
    fps = recipe.get("fps", 15)
    fps = _finite_number(fps, "fps")
    if kind == "clip" and not 1 <= fps <= MAX_FPS:
        raise VisualExtractionError(f"Clip frame sampling must be between 1 and {MAX_FPS} fps")
    if kind == "image" and fps != 15:
        raise VisualExtractionError("fps applies to clips only")
    crop = recipe.get("crop")
    if crop is not None:
        if not isinstance(crop, dict) or set(crop) != {"x", "y", "width", "height"}:
            raise VisualExtractionError("crop requires normalized x, y, width, and height")
        crop = {key: _finite_number(value, f"crop.{key}") for key, value in crop.items()}
        if not (0 <= crop["x"] < 1 and 0 <= crop["y"] < 1 and
                0 < crop["width"] <= 1 and 0 < crop["height"] <= 1 and
                crop["x"] + crop["width"] <= 1 and crop["y"] + crop["height"] <= 1):
            raise VisualExtractionError("crop must be a non-empty rectangle within the frame")
    return {"kind": kind, "start": start, "end": end, "max_width": max_width,
            "source_resolution": source_resolution, "fps": fps, "crop": crop}


def _frames_in_window(info: SourceInfo, start: float, end: float, *,
                      from_start: bool = False) -> list[tuple[float, float]]:
    margin = 2.0
    read_start = max(0.0, start - margin)
    read_end = min(info.duration, end + margin)
    absolute_start = info.container_start_time + read_start
    if from_start:
        # An omitted interval start avoids seeking even to the nominal origin:
        # MPEG-TS can otherwise land after the only usable keyframe.
        rate = max(info.avg_frame_rate, info.nominal_frame_rate)
        if (not rate or read_end > MAX_PREFIX_DECODE_SECONDS
                or math.ceil(read_end * rate) > MAX_PROBE_FRAMES
                or math.ceil(read_end * rate) * info.coded_width * info.coded_height > MAX_DECODED_PIXELS):
            raise VisualExtractionError("Source seeking failed and decoding from the beginning exceeds the safe prefix budget")
        # Bound actual work even if source rate/duration metadata is dishonest.
        # ffprobe applies the count to the selected video stream. Insufficient
        # coverage fails closed instead of widening it or changing the frame.
        packet_limit = min(MAX_PROBE_FRAMES,
                           MAX_DECODED_PIXELS // (info.coded_width * info.coded_height))
        interval = f"%+#{packet_limit}"
    else:
        # A relative end would be measured from the demuxer's actual seek
        # landing, which may precede read_start by a whole GOP.
        interval = f"{absolute_start:.6f}%{info.container_start_time + read_end:.6f}"
    data = _probe_json(
        ["-select_streams", "v:0", "-read_intervals", interval, "-show_frames",
         "-show_entries", "frame=best_effort_timestamp_time,pkt_duration_time", str(info.path)],
        timeout=min(60, 10 + max(0, read_end if from_start else read_end - read_start) * 1.5),
    )
    frames = []
    for frame in data.get("frames", []):
        try:
            pts = _finite_number(frame.get("best_effort_timestamp_time"), "Frame timestamp") - info.container_start_time
            duration = _finite_number(frame.get("pkt_duration_time") or 0, "Frame duration")
        except VisualExtractionError:
            continue
        if start <= pts < end:
            frames.append((pts, max(0, duration)))
    if not frames:
        raise _NoSourceFrames("No decodable video frame exists in the requested time range")
    if len(frames) > MAX_PROBE_FRAMES:
        raise VisualExtractionError("Requested range contains too many frames to process safely")
    if len(frames) * info.coded_width * info.coded_height > MAX_DECODED_PIXELS:
        raise VisualExtractionError("Requested range exceeds the decoded-pixel budget; shorten it or choose a smaller source")
    return frames


def _crop_pixels(crop: dict | None, info: SourceInfo, *, even: bool) -> tuple[int, int, int, int] | None:
    if crop is None:
        return None
    width, height = info.display_width, info.display_height
    x = math.floor(crop["x"] * width)
    y = math.floor(crop["y"] * height)
    w = math.floor(crop["width"] * width)
    h = math.floor(crop["height"] * height)
    if even:
        x, y, w, h = x - x % 2, y - y % 2, w - w % 2, h - h % 2
    if w < (2 if even else 1) or h < (2 if even else 1) or x + w > width or y + h > height:
        raise VisualExtractionError("Crop is too small after converting it to source pixels")
    return x, y, w, h


def _video_filter(info: SourceInfo, crop: dict | None, max_width: int, *, even: bool,
                  source_resolution: bool = False, fps: float | None = None) -> str:
    filters = []
    pixels = _crop_pixels(crop, info, even=even)
    if pixels:
        x, y, width, height = pixels
        filters.append(f"crop={width}:{height}:{x}:{y}")
    else:
        width, height = info.display_width, info.display_height
    if fps is not None:
        # Ignore binary floating-point noise at an exact cadence boundary.
        filters.append(f"select='if(isnan(prev_selected_t),1,gte(t-prev_selected_t,{1.0 / fps:.12f}-0.000000001))'")
    # Normalize non-square source pixels after display-space crop. Shrink only
    # the axis that needs correction, then apply the width/height budget.
    sar = info.display_sample_aspect_ratio
    square_width = width * min(1.0, sar)
    square_height = height / max(1.0, sar)
    width_limit = 8192 if source_resolution else max_width
    scale = min(1.0, width_limit / square_width, 8192 / square_height)
    target_width = max(2 if even else 1, math.floor(square_width * scale))
    target_height = max(2 if even else 1, math.floor(square_height * scale))
    if even:
        target_width -= target_width % 2
        target_height -= target_height % 2
    if (target_width, target_height) != (width, height) or abs(sar - 1.0) > 1e-6:
        filters.append(f"scale={target_width}:{target_height}:flags=lanczos")
    filters.append("setsar=1")
    return ",".join(filters)


def _output_probe(path: Path) -> tuple[dict, list[float]]:
    metadata = _probe_json(["-show_format", "-show_streams", "-show_frames",
                            "-show_entries", "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate,sample_aspect_ratio,display_aspect_ratio:frame=best_effort_timestamp_time",
                            str(path)], timeout=PROBE_TIMEOUT_SECONDS)
    streams = [s for s in metadata.get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        raise VisualExtractionError("FFmpeg output has no decodable video stream")
    pts = []
    for frame in metadata.get("frames", []):
        try:
            pts.append(_finite_number(frame.get("best_effort_timestamp_time"), "Output timestamp"))
        except VisualExtractionError:
            continue
    if not pts:
        raise VisualExtractionError("FFmpeg output contains no decodable frames")
    return {"format": metadata.get("format", {}), "stream": streams[0]}, pts


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _display_ratio(value: str | None, width: int, height: int, sample_aspect: str | None) -> float:
    if value:
        parts = value.split(":", 1)
        try:
            if len(parts) == 2 and float(parts[1]) != 0:
                ratio = float(parts[0]) / float(parts[1])
            else:
                ratio = float(value)
            if math.isfinite(ratio) and ratio > 0:
                return ratio
        except (ValueError, ZeroDivisionError):
            pass
    sar = _rate(sample_aspect or "1:1")
    ratio = width * (sar or 1) / max(height, 1)
    return ratio if math.isfinite(ratio) and ratio > 0 else width / max(height, 1)


def _worker_manifest(result: dict) -> dict:
    """Project extraction details to the versioned worker API manifest contract."""
    return {key: value for key, value in result.items() if key in WORKER_MANIFEST_FIELDS}


def extract(source: Path, output_dir: Path, recipe: dict) -> dict:
    """Extract one bounded still or muted MP4 from a trusted retained source.

    Recipes use normalized crop coordinates in the rotated display space. For
    clips, `fps` is a sampling ceiling: source frames are selected by PTS and
    output uses VFR timestamps so frames are never synthesized or duplicated.
    """
    spec = _validate_recipe(recipe)
    info = probe_source(source)
    if spec["start"] < 0 or spec["start"] >= info.duration:
        raise VisualExtractionError("Requested timestamp is outside the source recording")
    if spec["kind"] == "clip" and spec["end"] > info.duration:
        raise VisualExtractionError("Requested clip ends beyond the source recording")
    source_rate_limit = info.nominal_frame_rate or info.avg_frame_rate
    if spec["kind"] == "clip" and not source_rate_limit:
        raise VisualExtractionError("Source frame-rate metadata is unavailable; clip extraction cannot be bounded safely")
    sampling_fps = min(spec["fps"], source_rate_limit) if spec["kind"] == "clip" else None

    decode_from_start = False
    search_end = spec["end"]
    if spec["kind"] == "image":
        search_span = max(1.0, min(5.0, 2.0 / max(info.avg_frame_rate, 0.1)))
        search_end = min(info.duration, spec["start"] + search_span)
    try:
        frames = _frames_in_window(info, spec["start"], search_end)
    except _NoSourceFrames:
        frames = _frames_in_window(info, spec["start"], search_end, from_start=True)
        decode_from_start = True
    if spec["kind"] == "image":
        actual_start = frames[0][0]
        actual_end = actual_start
    else:
        actual_start = frames[0][0]
        last_pts, last_duration = frames[-1]
        actual_end = min(spec["end"], last_pts + (last_duration or 0))
        if decode_from_start and actual_end + 0.000001 < spec["end"]:
            raise VisualExtractionError("Bounded prefix decoding did not cover the requested clip")
    if actual_start - spec["start"] > 1.0:
        raise VisualExtractionError("No frame is available within one second of the requested source time")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve(strict=True)
    filename = "image.jpg" if spec["kind"] == "image" else "clip.mp4"
    final_path = output_dir / filename
    if final_path.exists():
        raise VisualExtractionError("Extraction output already exists")
    temporary = output_dir / f".{filename}.{uuid.uuid4().hex}.partial{Path(filename).suffix}"
    output_budget = MAX_IMAGE_BYTES if spec["kind"] == "image" else MAX_CLIP_BYTES
    input_seek = (["-copyts", "-i", str(info.path)] if decode_from_start else
                  ["-seek_timestamp", "1", "-ss", f"{info.container_start_time + actual_start:.9f}",
                   "-i", str(info.path)])
    prefix_trim = (f"trim=start={info.container_start_time + actual_start:.9f}:"
                   f"end={info.container_start_time + search_end:.9f},setpts=PTS-STARTPTS,"
                   if decode_from_start else "")

    if spec["kind"] == "image":
        video_filter = _video_filter(info, spec["crop"], spec["max_width"], even=False,
                                     source_resolution=spec["source_resolution"])
        video_filter = prefix_trim + video_filter
        args = ["ffmpeg", "-nostdin", "-v", "error", "-threads", "2", "-protocol_whitelist", source_protocols(info.path),
                "-format_whitelist", FORMAT_WHITELIST, *input_seek,
                "-map", "0:v:0", "-an", "-frames:v", "1", "-vf", video_filter,
                "-q:v", "2", "-fs", str(output_budget), "-y", str(temporary)]
    else:
        video_filter = _video_filter(info, spec["crop"], spec["max_width"], even=True, fps=sampling_fps)
        video_filter = prefix_trim + video_filter
        args = ["ffmpeg", "-nostdin", "-v", "error", "-threads", "2", "-protocol_whitelist", source_protocols(info.path),
                "-format_whitelist", FORMAT_WHITELIST, *input_seek,
                "-map", "0:v:0", "-an", "-t", f"{spec['end'] - actual_start:.9f}",
                "-vf", video_filter, "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-fs", str(output_budget),
                "-y", str(temporary)]
    try:
        _run(args, timeout=EXTRACTION_TIMEOUT_SECONDS)
        if not temporary.is_file() or temporary.stat().st_size <= 0 or temporary.stat().st_size >= output_budget:
            raise VisualExtractionError("Extracted media exceeded the output-byte limit")
        output_meta, output_pts = _output_probe(temporary)
        os.replace(temporary, final_path)
    finally:
        temporary.unlink(missing_ok=True)

    stream = output_meta["stream"]
    width, height = int(stream["width"]), int(stream["height"])
    if width * height > MAX_SOURCE_FRAME_PIXELS:
        final_path.unlink(missing_ok=True)
        raise VisualExtractionError("Extracted frame exceeds the decoded-pixel limit")
    output_fps = _rate(stream.get("avg_frame_rate"))
    if spec["kind"] == "clip":
        # VFR average_frame_rate counts the final packet's short duration and
        # can exceed the selected cadence. Validate actual inter-frame PTS.
        gaps = [b - a for a, b in zip(output_pts, output_pts[1:])]
        if any(gap <= 0 for gap in gaps):
            final_path.unlink(missing_ok=True)
            raise VisualExtractionError("Encoded clip contains duplicate or reversed frame timestamps")
        cadence = 1 / min(gaps) if gaps else min(output_fps or sampling_fps, sampling_fps)
        if cadence > sampling_fps + 0.001:
            final_path.unlink(missing_ok=True)
            raise VisualExtractionError("Encoded frame rate exceeded the requested sampling ceiling")
        output_fps = min(sampling_fps, round(cadence, 6))
    bytes_written = final_path.stat().st_size
    return {
        "filename": filename,
        "kind": spec["kind"],
        "requested_start": spec["start"],
        "requested_end": spec["end"],
        "actual_start": actual_start,
        "actual_end": actual_end,
        "width": width,
        "height": height,
        "output_sample_aspect_ratio": stream.get("sample_aspect_ratio") or "1:1",
        "output_display_aspect_ratio": _display_ratio(
            stream.get("display_aspect_ratio"), width, height, stream.get("sample_aspect_ratio")),
        "fps": output_fps if spec["kind"] == "clip" else None,
        "requested_fps": spec["fps"] if spec["kind"] == "clip" else None,
        "sampling_fps_ceiling": sampling_fps,
        "output_frame_count": len(output_pts),
        "source_width": info.display_width,
        "source_height": info.display_height,
        "source_fps": info.avg_frame_rate or None,
        "source_avg_frame_rate": info.avg_frame_rate or None,
        "source_nominal_frame_rate": info.nominal_frame_rate or None,
        "source_variable_frame_rate": info.variable_frame_rate,
        "source_rotation_degrees": info.rotation_degrees,
        "source_origin_time": info.container_start_time,
        "source_video_start_time": info.video_start_time,
        "rotation": float(info.rotation_degrees),
        "rotation_applied": bool(info.rotation_degrees),
        "source_sample_aspect_ratio": info.sample_aspect_ratio,
        "source_display_sample_aspect_ratio": info.display_sample_aspect_ratio,
        "source_display_aspect_ratio": info.display_aspect_ratio,
        "source_coded_width": info.coded_width,
        "source_coded_height": info.coded_height,
        "source_pts_start": actual_start,
        "source_pts_end": actual_end,
        "crop": spec["crop"],
        "recipe_version": RECIPE_VERSION,
        "sha256": _sha256(final_path),
        "bytes": bytes_written,
    }


def main(argv: list[str] | None = None) -> int:
    """Worker subprocess entry point; all paths are supplied by trusted worker code."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--leased-source")
    parser.add_argument("--source-bytes",type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True, help="JSON extraction recipe")
    parser.add_argument("--manifest", type=Path, required=True, help="JSON output metadata path")
    args = parser.parse_args(argv)
    try:
        recipe = json.loads(args.recipe.read_text())
        if bool(args.source)==bool(args.leased_source):raise VisualExtractionError('Choose one source')
        source=LeasedSource(args.leased_source,args.source_bytes or 0) if args.leased_source else args.source
        result = extract(source, args.output, recipe)
        manifest = _worker_manifest(result)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.manifest.with_name(f".{args.manifest.name}.{uuid.uuid4().hex}.partial")
        try:
            temporary.write_text(json.dumps(manifest, separators=(",", ":")) + "\n")
            os.replace(temporary, args.manifest)
        finally:
            temporary.unlink(missing_ok=True)
        print(json.dumps(manifest, separators=(",", ":")))
        return 0
    except (OSError, json.JSONDecodeError, VisualExtractionError) as exc:
        print(json.dumps({"error": str(exc)}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
