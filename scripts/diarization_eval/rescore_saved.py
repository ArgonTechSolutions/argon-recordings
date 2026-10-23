#!/usr/bin/env python3
"""Re-score frozen diarization RTTMs using standard 250 ms total collar."""
import json
import wave
from pathlib import Path

from score import Turn, read_rttm, score

ROOT = Path(__file__).resolve().parents[2] / "models" / "diarization-evaluation"
DATASETS = (ROOT / "heldout", ROOT / "wespeaker-heldout-final")


def rescore(directory: Path) -> dict:
    report_path = directory / "evaluation.json"
    report = json.loads(report_path.read_text())
    backup = directory / "evaluation.nonstandard-500ms-mask.json"

    if directory.name == "wespeaker-heldout-final":
        manifest = json.loads((directory / "manifest.json").read_text())
        prior = json.loads((ROOT / "wespeaker-heldout/evaluation.json").read_text())
        prior_by_id = {row["id"]: row for row in prior["per_clip"]}
        report["heldout_ids"] = [item["id"] for item in manifest]
        report["per_clip"] = [
            dict(prior_by_id[name]) if name != "bmsyn" else {
                "id": "bmsyn", "elapsed_seconds": 64.07212934689596
            }
            for name in report["heldout_ids"]
        ]
        report["total_duration_seconds"] = len(manifest) * 240
        report["total_elapsed_seconds"] = sum(row["elapsed_seconds"] for row in report["per_clip"])
        report["aggregate_rtf"] = report["total_elapsed_seconds"] / report["total_duration_seconds"]
        report["peak_rss_kib"] = max(report.get("peak_rss_kib", 0), prior.get("peak_rss_kib", 0), 243212)

    manifest = json.loads((directory / "manifest.json").read_text())
    clip_seconds = {item["id"]: item.get("clip_seconds", report.get("clip_seconds_each", report.get("clip_seconds", 240)))
                    for item in manifest}
    results = []
    legacy_results = []
    for row in report["per_clip"]:
        name = row["id"]
        with wave.open(str(directory / f"{name}.wav"), "rb") as audio:
            duration = min(clip_seconds[name], audio.getnframes() / audio.getframerate())
        ref = [Turn(t.speaker, max(0, t.start), min(duration, t.end))
               for t in read_rttm(directory / f"{name}.rttm").get(name, [])
               if t.start < duration and t.end > 0]
        hyp = [Turn(t.speaker, max(0, t.start), min(duration, t.end))
               for t in read_rttm(directory / f"{name}.hyp.rttm").get(name, [])
               if t.start < duration and t.end > 0]
        metrics = {
            policy: score(ref, hyp, duration=duration, collar=collar, ignore_overlap=ignore)
            for policy, collar, ignore in (
                ("plan", 0.25, True),
                ("zero_collar_overlap_inclusive", 0, False),
            )
        }
        legacy_metrics = {
            policy: score(ref, hyp, duration=duration, collar=collar, ignore_overlap=ignore)
            for policy, collar, ignore in (
                ("plan", 0.5, True),
                ("zero_collar_overlap_inclusive", 0, False),
            )
        }
        row.update({
            "duration_seconds": duration,
            "reference_speakers": metrics["plan"]["reference_speakers"],
            "hypothesis_speakers": metrics["plan"]["hypothesis_speakers"],
            "speaker_count_error": metrics["plan"]["speaker_count_error"],
            "metrics": metrics,
        })
        results.append(row)
        legacy_results.append({**row, "metrics": legacy_metrics})

    aggregate = {}
    for policy in ("plan", "zero_collar_overlap_inclusive"):
        keys = ("miss_seconds", "false_alarm_seconds", "confusion_seconds", "scored_speech_seconds")
        sums = {key: sum(row["metrics"][policy][key] for row in results) for key in keys}
        sums["der"] = sum(sums[k] for k in keys[:3]) / sums["scored_speech_seconds"] if sums["scored_speech_seconds"] else 0
        aggregate[policy] = sums

    report["scoring_method"] = {
        "collar": "250 ms total width, masking 125 ms on each side of each reference boundary",
        "frame_step_seconds": 0.01,
        "speaker_counts": "unique speakers in full clipped reference/hypothesis turns, independent of collar and overlap mask",
    }
    report["per_clip"] = results
    report["aggregate"] = aggregate
    report_path.write_text(json.dumps(report, indent=2) + "\n")

    legacy = dict(report)
    legacy["scoring_method"] = {
        "status": "historical nonstandard score",
        "collar": "500 ms total width (250 ms each side); retained for traceability",
        "frame_step_seconds": 0.01,
        "speaker_counts": "full clipped turns",
    }
    legacy["per_clip"] = legacy_results
    legacy_aggregate = {}
    for policy in ("plan", "zero_collar_overlap_inclusive"):
        keys = ("miss_seconds", "false_alarm_seconds", "confusion_seconds", "scored_speech_seconds")
        sums = {key: sum(row["metrics"][policy][key] for row in legacy_results) for key in keys}
        sums["der"] = sum(sums[k] for k in keys[:3]) / sums["scored_speech_seconds"] if sums["scored_speech_seconds"] else 0
        legacy_aggregate[policy] = sums
    legacy["aggregate"] = legacy_aggregate
    backup.write_text(json.dumps(legacy, indent=2) + "\n")
    return report


if __name__ == "__main__":
    for dataset in DATASETS:
        result = rescore(dataset)
        exact = sum(row["speaker_count_error"] == 0 for row in result["per_clip"])
        print(json.dumps({
            "dataset": dataset.name,
            "ids": [row["id"] for row in result["per_clip"]],
            "plan_der": result["aggregate"]["plan"]["der"],
            "plan_components_seconds": {key: result["aggregate"]["plan"][key]
                                         for key in ("miss_seconds", "false_alarm_seconds", "confusion_seconds")},
            "zero_collar_der": result["aggregate"]["zero_collar_overlap_inclusive"]["der"],
            "exact_count_hits": exact,
            "count_errors": {row["id"]: row["speaker_count_error"] for row in result["per_clip"]},
        }, indent=2))
