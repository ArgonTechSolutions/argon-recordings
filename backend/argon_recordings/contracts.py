from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class ProjectCreate(Strict):
    name: str = Field(min_length=1, max_length=100, pattern=r'\S')


class Project(Strict):
    id: str
    name: str
    created: float
    recording_count: int = 0


class RecordingCreate(Strict):
    title: str = Field(min_length=1, max_length=150, pattern=r'\S')
    filename: str = Field(min_length=1, max_length=255)
    language: Literal['es', 'en', 'auto'] = 'auto'
    priority: Literal['normal', 'urgent'] = 'normal'


class Job(Strict):
    id: str
    state: str
    progress: float
    stage: str


class Artifact(Strict):
    name: str
    kind: Literal['audio', 'frame']
    time: float | None = None
    url: str


class Recording(Strict):
    id: str
    project_id: str
    title: str
    filename: str
    language: str
    status: str
    bytes: int
    sha256: str | None
    duration: float | None
    error: str | None
    created: float
    job: Job | None = None
    artifacts: list[Artifact] = []


class Word(Strict):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    word: str = Field(max_length=200)
    probability: float = Field(ge=0, le=1)


class Segment(Strict):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(min_length=1, max_length=8192)
    words: list[Word] = Field(default_factory=list, max_length=1000)

    @model_validator(mode='after')
    def ordered(self):
        if self.end < self.start or any(w.end < w.start or w.start < self.start - .1 or w.end > self.end + .1 for w in self.words):
            raise ValueError('Invalid segment or word interval')
        return self


class Transcript(Strict):
    recording_id: str
    segments: list[Segment]


class ClaimRequest(Strict):
    worker: str = Field(min_length=1, max_length=64)


class Heartbeat(Strict):
    progress: float = Field(ge=0, le=1)
    stage: str = Field(min_length=1, max_length=80)


class ArtifactInput(Strict):
    name: str = Field(pattern=r'^(audio\.wav|frame-[0-9]{4}\.jpg)$')
    kind: Literal['audio', 'frame']
    time: float | None = Field(default=None, ge=0)


class Completion(Strict):
    duration: float = Field(gt=0, le=300)
    segments: list[Segment] = Field(max_length=1000)
    artifacts: list[ArtifactInput] = Field(max_length=25)

    @model_validator(mode='after')
    def valid(self):
        if any(s.end > self.duration + .1 for s in self.segments):
            raise ValueError('Segment exceeds recording duration')
        if any(b.start < a.start for a, b in zip(self.segments, self.segments[1:])):
            raise ValueError('Segments must be time ordered')
        names = [a.name for a in self.artifacts]
        if len(set(names)) != len(names) or names.count('audio.wav') != 1:
            raise ValueError('One audio artifact and unique names required')
        for a in self.artifacts:
            if (a.name == 'audio.wav') != (a.kind == 'audio'):
                raise ValueError('Artifact type/name mismatch')
            if a.kind == 'frame' and (a.time is None or a.time > self.duration):
                raise ValueError('Invalid frame time')
        return self


class Failure(Strict):
    message: str = Field(min_length=1, max_length=500)


class SearchHit(Strict):
    recording_id: str
    title: str
    start: float
    end: float
    text: str
