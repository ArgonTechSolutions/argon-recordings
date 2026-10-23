#!/usr/bin/env python3
"""Deterministic RTTM DER/count scorer for the diarization pilot."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Turn:
    speaker: str
    start: float
    end: float


def _max_assignment(weights: list[list[int]]) -> int:
    """Maximum weight one-to-one assignment using the O(n^3) Hungarian method."""
    rows, cols = len(weights), max((len(row) for row in weights), default=0)
    n = max(rows, cols)
    if n == 0:
        return 0
    max_weight = max((w for row in weights for w in row), default=0)
    cost = [[max_weight - (weights[i][j] if i < rows and j < len(weights[i]) else 0)
             for j in range(n)] for i in range(n)]
    u, v, p, way = [0] * (n + 1), [0] * (n + 1), [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], float("inf"), 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += int(delta)
                    v[j] -= int(delta)
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return sum(weights[i - 1][j - 1] for j, i in enumerate(p) if i and i <= rows and j and j <= cols)


def read_rttm(path: str | Path) -> dict[str, list[Turn]]:
    result: dict[str, list[Turn]] = {}
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if not fields or fields[0] != "SPEAKER":
            continue
        recording, start, duration, speaker = fields[1], float(fields[3]), float(fields[4]), fields[7]
        result.setdefault(recording, []).append(Turn(speaker, start, start + duration))
    return result


def score(reference: Iterable[Turn], hypothesis: Iterable[Turn], *, duration: float,
          collar: float = 0.25, ignore_overlap: bool = True, step: float = 0.01) -> dict:
    """Return miss/FA/confusion seconds, DER and speaker-count delta.

    ``collar`` is pyannote's total collar width; half is masked on each side of
    every reference boundary. Overlap frames are excluded only when requested.
    Mapping is permutation invariant.
    """
    ref, hyp = list(reference), list(hypothesis)
    n = int(duration / step)
    radius = collar / 2
    boundaries = [t for x in ref for t in (x.start, x.end)]
    labels_r, labels_h = [], []
    for i in range(n):
        t = (i + .5) * step
        if any(b - radius <= t < b + radius for b in boundaries):
            continue
        rs = {x.speaker for x in ref if x.start <= t < x.end}
        hs = {x.speaker for x in hyp if x.start <= t < x.end}
        if ignore_overlap and len(rs) > 1:
            continue
        labels_r.append(rs)
        labels_h.append(hs)
    rnames = sorted({s for ss in labels_r for s in ss})
    hnames = sorted({s for ss in labels_h for s in ss})
    # Global permutation-invariant mapping from reference to hypothesis IDs.
    matrix = [[sum(1 for r, h in zip(labels_r, labels_h) if a in r and b in h)
               for b in hnames] for a in rnames]
    correct_frames = _max_assignment(matrix)
    reference_speakers = {x.speaker for x in ref if x.end > 0 and x.start < duration and x.end > x.start}
    hypothesis_speakers = {x.speaker for x in hyp if x.end > 0 and x.start < duration and x.end > x.start}
    ref_speech = sum(len(s) for s in labels_r)
    miss_frames = sum(max(0, len(r) - len(h)) for r, h in zip(labels_r, labels_h))
    false_alarm_frames = sum(max(0, len(h) - len(r)) for r, h in zip(labels_r, labels_h))
    confusion_frames = sum(min(len(r), len(h)) for r, h in zip(labels_r, labels_h)) - correct_frames
    total = ref_speech * step
    miss, false_alarm, confusion = miss_frames * step, false_alarm_frames * step, confusion_frames * step
    return {"miss_seconds": miss, "false_alarm_seconds": false_alarm,
            "confusion_seconds": confusion, "der": (miss + false_alarm + confusion) / total if total else 0,
            "reference_speakers": len(reference_speakers),
            "hypothesis_speakers": len(hypothesis_speakers),
            "speaker_count_error": len(hypothesis_speakers) - len(reference_speakers),
            "scored_speech_seconds": total}
