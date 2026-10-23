import math

import pytest
from pydantic import ValidationError

from argon_recordings.timeline import (
    EvidenceVersions, SourceReference, TimelineRange, evidence_key,
)


@pytest.mark.parametrize('start,end', [(0, 0), (0, 3), (1.25, 2.75)])
def test_points_and_intervals_preserve_source_seconds(start, end):
    interval = TimelineRange(start=start, end=end)
    assert interval.within(3) is interval
    assert (interval.start, interval.end) == (start, end)


@pytest.mark.parametrize('start,end', [(-1, 1), (3, 2), (math.nan, 1),
                                     (0, math.inf), (-math.inf, 0)])
def test_invalid_timelines_rejected(start, end):
    with pytest.raises(ValidationError):
        TimelineRange(start=start, end=end)


@pytest.mark.parametrize('duration', [1, -1, math.inf, math.nan])
def test_duration_checked_before_scheduling(duration):
    with pytest.raises(ValueError):
        TimelineRange(start=1, end=2).within(duration)


def reference(**updates):
    return SourceReference.model_validate({
        'recording_id': 'recording-a', 'source_sha256': 'a' * 64,
        'interval': {'start': 0, 'end': 2}, 'versions': {}, **updates,
    })


def test_historical_absence_defaults_to_unenriched_revisions():
    assert EvidenceVersions().model_dump() == {
        'transcript': 0, 'speakers': 0, 'annotations': 0, 'extraction': 0,
    }


@pytest.mark.parametrize('updates', [
    {'source_sha256': 'b' * 64}, {'recording_id': 'recording-b'},
    {'interval': {'start': 1, 'end': 2}}, {'versions': {'speakers': 1}},
    {'versions': {'transcript': 1}}, {'versions': {'annotations': 1}},
    {'versions': {'extraction': 1}},
])
def test_stale_or_different_evidence_cannot_share_identity(updates):
    old, new = reference(), reference(**updates)
    assert not old.matches(new)
    assert evidence_key(old, {'kind': 'still'}) != evidence_key(new, {'kind': 'still'})


def test_recipe_identity_stable_across_restart_and_key_order():
    original = reference()
    restored = SourceReference.model_validate_json(original.model_dump_json())
    assert original.matches(restored)
    assert evidence_key(original, {'kind': 'still', 'width': 1280}) == evidence_key(
        restored, {'width': 1280, 'kind': 'still'})
    assert evidence_key(original, {'width': 1280}) != evidence_key(original, {'width': 1920})


def test_invalid_hash_and_unknown_fields_fail_closed():
    with pytest.raises(ValidationError):
        reference(source_sha256='../source')
    with pytest.raises(ValidationError):
        TimelineRange(start=0, end=1, path='/tmp/arbitrary')
    with pytest.raises(ValidationError):
        EvidenceVersions(transcript=-1)
    with pytest.raises(ValueError):
        evidence_key(reference(), {'width': math.nan})
