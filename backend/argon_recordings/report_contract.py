"""Portable report/reference checks. Semantic truth and actual viewing remain agent/human gates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

VERSION = "argon-summary-v1"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_DRAFT_BYTES = 1024 * 1024
ID = r"^[a-f0-9]{32}$"
SHA = r"^[a-f0-9]{64}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_max_length=8192)


class Interval(Strict):
    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("interval order")
        return self

    def contains(self, other):
        return self.start <= other.start <= other.end <= self.end


class Versions(Strict):
    transcript: int = Field(ge=0)
    speakers: int = Field(ge=0)
    annotations: int = Field(ge=0)
    extraction: int = Field(ge=0)


class Reference(Strict):
    recording_id: str = Field(pattern=ID)
    source_sha256: str = Field(pattern=SHA)
    interval: Interval
    versions: Versions


class Segment(Interval):
    id: str = Field(min_length=1, max_length=128)
    text: str


class TranscriptPage(Strict):
    cursor: str | None = Field(default=None, max_length=2048)
    next_cursor: str | None = Field(default=None, max_length=2048)
    reference: Reference
    items: list[Segment] = Field(max_length=40)


class InspectedImage(Strict):
    id: str = Field(pattern=r"^(?:asset:[a-f0-9]{32}|frame:frame-[0-9]{4}\.jpg)$")
    timestamp: float = Field(ge=0)
    source_sha256: str = Field(pattern=SHA)
    sha256: str = Field(pattern=SHA)
    recipe_version: str = Field(min_length=1, max_length=64)
    inspected: bool


class Evidence(Strict):
    reference: Reference
    project_id: str = Field(pattern=ID)
    transcript_pages: list[TranscriptPage] = Field(min_length=1, max_length=500)
    images: list[InspectedImage] = Field(default_factory=list, max_length=1000)


class Citation(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    kind: Literal["transcript", "image"]
    evidence_id: str = Field(min_length=1, max_length=128)
    interval: Interval


class GroundedValue(Strict):
    text: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=2000)
    citation_ids: list[str] = Field(min_length=1, max_length=16)


class Item(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    kind: Literal["summary", "chapter", "decision", "action", "question"]
    basis: Literal["explicit", "inference", "unresolved"]
    text: str = Field(min_length=1)
    citation_ids: list[str] = Field(default_factory=list, max_length=16)
    visual_observation: bool = False
    owner: GroundedValue | None = None
    due: GroundedValue | None = None


class Report(Strict):
    template_version: Literal["argon-summary-v1"] = VERSION
    reference: Reference
    scope: Interval
    language: str = Field(min_length=2, max_length=35, pattern=r"^[a-zA-Z]+(?:-[a-zA-Z0-9]+)*$")
    coverage: Literal["complete", "partial"]
    limitations: list[str] = Field(default_factory=list, max_length=100)
    citations: list[Citation] = Field(max_length=1000)
    items: list[Item] = Field(min_length=1, max_length=1000)


class Generator(Strict):
    name: str = Field(min_length=1, max_length=80, pattern=r'\S')
    version: str = Field(min_length=1, max_length=64, pattern=r'\S')
    model: str | None = Field(default=None, max_length=80)


class ImageAcknowledgement(Strict):
    id: str = Field(pattern=r"^(?:asset:[a-f0-9]{32}|frame:frame-[0-9]{4}\.jpg)$")
    sha256: str = Field(pattern=SHA)
    inspected: Literal[True]


class ReportDraft(Strict):
    title: str = Field(min_length=1, max_length=150, pattern=r'\S')
    generator: Generator
    report: Report
    inspected_images: list[ImageAcknowledgement] = Field(default_factory=list, max_length=16)


class ContractError(ValueError):
    """Constant diagnostics deliberately exclude source text and credentials."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def unique(values):
    require(len(values) == len(set(values)), "duplicate evidence or item IDs")


def validate_report(report: Report, evidence: Evidence, current: Reference) -> None:
    require(report.reference == evidence.reference == current, "stale or mismatched evidence snapshot")
    require(current.interval.contains(report.scope), "scope exceeds recording")
    segments = {}
    cursor = None
    seen_cursors = set()
    previous_start = -1
    for index, page in enumerate(evidence.transcript_pages):
        require(page.cursor == cursor, "broken transcript cursor chain")
        require(index == 0 or page.cursor is not None, "transcript pages continue after completion")
        require(page.reference.recording_id == current.recording_id and
                page.reference.source_sha256 == current.source_sha256 and
                page.reference.interval == current.interval and
                page.reference.versions.transcript == current.versions.transcript,
                "stale or mismatched transcript page")
        for segment in page.items:
            require(segment.id not in segments, "duplicate transcript segment")
            require(current.interval.contains(segment) and segment.start >= previous_start,
                    "unordered or out-of-range transcript segment")
            # Segments may overlap; order by source start, as the API does.
            previous_start = segment.start
            segments[segment.id] = segment
        cursor = page.next_cursor
        if cursor is not None:
            require(cursor not in seen_cursors and bool(cursor), "repeated or empty transcript cursor")
            seen_cursors.add(cursor)
    require(report.coverage != "complete" or cursor is None, "incomplete transcript coverage")
    require(report.coverage != "partial" or any(s.strip() for s in report.limitations),
            "partial summary needs a coverage limitation")
    validate_citations(report, current, segments, evidence.images)


def validate_citations(report: Report, current: Reference, segments: dict[str, Segment],
                       selected_images: list[InspectedImage]) -> None:
    """Validate selected evidence without asserting reading coverage."""
    require(report.reference == current, "stale or mismatched evidence snapshot")
    require(current.interval.contains(report.scope), "scope exceeds recording")
    require(report.coverage != "partial" or any(s.strip() for s in report.limitations),
            "partial summary needs a coverage limitation")
    unique([image.id for image in selected_images])
    images = {image.id: image for image in selected_images}
    for image in images.values():
        require(image.source_sha256 == current.source_sha256 and
                current.interval.start <= image.timestamp <= current.interval.end,
                "mismatched or out-of-range image")
    unique([citation.id for citation in report.citations])
    citations = {citation.id: citation for citation in report.citations}
    for citation in citations.values():
        require(report.scope.contains(citation.interval), "citation exceeds summary scope")
        if citation.kind == "transcript":
            segment = segments.get(citation.evidence_id)
            require(segment is not None and segment.contains(citation.interval),
                    "unknown or out-of-range transcript citation")
        else:
            image = images.get(citation.evidence_id)
            require(image is not None and image.inspected, "image citation was not inspected")
            require(citation.interval.start == citation.interval.end == image.timestamp,
                    "image citation must use actual source timestamp")
    unique([item.id for item in report.items])
    for item in report.items:
        require(all(cid in citations for cid in item.citation_ids), "unknown item citation")
        require(bool(item.citation_ids) or item.basis == "unresolved", "claim requires source evidence")
        require(not item.visual_observation or any(citations[cid].kind == "image" for cid in item.citation_ids),
                "visual claim needs an inspected image")
        require(item.kind == "action" or (item.owner is None and item.due is None),
                "ownership and due dates apply only to actions")
        for value in (item.owner, item.due):
            if value is None:
                continue
            require(item.basis == "explicit", "inferred action cannot assert owner or due date")
            require(all(cid in item.citation_ids and citations[cid].kind == "transcript"
                        for cid in value.citation_ids), "assignment needs an action transcript citation")
            require(all(citations[cid].interval.start == segments[citations[cid].evidence_id].start and
                        citations[cid].interval.end == segments[citations[cid].evidence_id].end
                        for cid in value.citation_ids), "assignment quote needs the containing segment interval")
            texts = [segments[citations[cid].evidence_id].text for cid in value.citation_ids]
            require(any(value.quote in text for text in texts) and
                    value.text.casefold() in value.quote.casefold(), "unsupported owner or due date literal")


def read_json(path):
    with Path(path).open("rb") as handle:
        raw = handle.read(MAX_INPUT_BYTES + 1)
    require(len(raw) <= MAX_INPUT_BYTES, "input exceeds 16 MiB")
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schema")
    for command in ("check", "draft"):
        check = sub.add_parser(command)
        for name in ("report", "evidence", "current_reference"):
            check.add_argument(name, type=Path)
        if command == "draft":
            check.add_argument('--title', required=True)
            check.add_argument('--generator-name', required=True)
            check.add_argument('--generator-version', required=True)
            check.add_argument('--model')
    args = parser.parse_args()
    if args.command == "schema":
        print(json.dumps({"report": Report.model_json_schema(), "evidence": Evidence.model_json_schema(),
                          "current_reference": Reference.model_json_schema(),
                          "draft": ReportDraft.model_json_schema()}, indent=2))
        return
    try:
        report = Report.model_validate(read_json(args.report))
        evidence = Evidence.model_validate(read_json(args.evidence))
        validate_report(report, evidence, Reference.model_validate(read_json(args.current_reference)))
        if args.command == 'draft':
            selected = {c.evidence_id for c in report.citations if c.kind == 'image'}
            draft = ReportDraft(title=args.title,
                generator=Generator(name=args.generator_name, version=args.generator_version, model=args.model),
                report=report, inspected_images=[ImageAcknowledgement(id=i.id, sha256=i.sha256, inspected=True)
                    for i in evidence.images if i.id in selected])
            output = draft.model_dump_json(indent=2)
            require(len(output.encode()) <= MAX_DRAFT_BYTES, "importable draft exceeds 1 MiB; reduce the report")
            print(output)
            return
    except (ContractError, ValidationError, ValueError, OSError) as exc:
        # ValidationError contains the input by default; never print it here.
        message = str(exc) if isinstance(exc, ContractError) else "invalid report or evidence input"
        parser.exit(1, message + "\n")
    print("Report references, coverage and literal assignments validated; semantic review remains required.")


if __name__ == "__main__":
    main()
