"""Source-time references shared by optional recording enrichments.

These contracts do not change the existing ASR publication protocol. A point is
represented by equal start/end; intervals use [start, end) source seconds.
"""
import hashlib
import json

from pydantic import Field, model_validator

from .contracts import Strict, EvidenceVersions


class TimelineRange(Strict):
    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @model_validator(mode='after')
    def ordered(self):
        if self.end < self.start:
            raise ValueError('Timeline end precedes start')
        return self

    def within(self, duration: float):
        # Validate duration through the same finite-number contract as times.
        boundary = TimelineRange(start=0, end=duration)
        if self.end > boundary.end:
            raise ValueError('Timeline range exceeds recording duration')
        return self


class SourceReference(Strict):
    recording_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    interval: TimelineRange
    versions: EvidenceVersions

    def matches(self, other: 'SourceReference') -> bool:
        """Exact snapshot identity, including the selected source interval."""
        return self == other


def evidence_key(reference: SourceReference, recipe: dict) -> str:
    """Content-address a validated reference and JSON extraction recipe.

    Reject non-finite recipe values; canonical key ordering permits reuse after
    restart. Actual output PTS belongs in the accepted artifact manifest, not the
    requested interval. Callers must validate recipe schemas before scheduling.
    """
    payload = {'reference': reference.model_dump(), 'recipe': recipe}
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()
