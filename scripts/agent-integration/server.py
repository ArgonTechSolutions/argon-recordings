"""Argon Recordings MCP stdio adapter. Credentials and API origin are process config, never tool input."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import stat
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver import Image
from mcp.types import ToolAnnotations
from pydantic import Field, model_validator

from pydantic import BaseModel, ConfigDict


MAX_JSON_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
ID_PATTERN = r"^[a-f0-9]{32}$"
TOKEN_PATTERN = r"^rec_agent_[A-Za-z0-9_-]{43}$"


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TimelineRange(ToolInput):
    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("interval end precedes start")
        return self


class EvidenceVersions(ToolInput):
    transcript: int = Field(default=0, ge=0)
    speakers: int = Field(default=0, ge=0)
    annotations: int = Field(default=0, ge=0)
    extraction: int = Field(default=0, ge=0)


class SourceReference(ToolInput):
    recording_id: str = Field(pattern=ID_PATTERN)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    interval: TimelineRange
    versions: EvidenceVersions


class Crop(ToolInput):
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def fits(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("crop exceeds the source frame")
        return self


class VisualRecipe(ToolInput):
    kind: Literal["image", "clip"] = "image"
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    max_width: int = Field(default=1920, ge=64, le=3840)
    source_resolution: bool = False
    fps: int = Field(default=15, ge=1, le=30)
    crop: Crop | None = None

    @model_validator(mode="after")
    def valid_recipe(self):
        if self.kind == "image" and self.start != self.end:
            raise ValueError("A screenshot requires a point in time")
        if self.kind == "clip" and not 0 < self.end - self.start <= 30:
            raise ValueError("Choose a clip between zero and 30 seconds")
        if self.kind == "clip" and self.source_resolution:
            raise ValueError("Source resolution is available for screenshots only")
        if self.kind == "image" and self.fps != 15:
            raise ValueError("Frame rate applies to clips only")
        return self


def _origin() -> tuple[str, bool]:
    raw = os.environ.get("ARGON_RECORDINGS_API_ORIGIN", "")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("ARGON_RECORDINGS_API_ORIGIN is invalid") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise RuntimeError("ARGON_RECORDINGS_API_ORIGIN must be an HTTP origin without path or credentials")
    if port is not None and not 1 <= port <= 65535:
        raise RuntimeError("ARGON_RECORDINGS_API_ORIGIN has an invalid port")
    loopback = parsed.hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme == "http" and not loopback:
        raise RuntimeError("HTTP API origins are allowed only on loopback; use HTTPS otherwise")
    return raw.rstrip("/"), loopback


def _token(origin_is_loopback: bool) -> str:
    env_token = os.environ.get("ARGON_AGENT_TOKEN")
    if env_token is not None:
        if (os.environ.get("ARGON_AGENT_ALLOW_ENV_TOKEN") != "1" or not origin_is_loopback
                or not re.fullmatch(TOKEN_PATTERN, env_token)):
            raise RuntimeError("Environment credentials are permitted only for explicit loopback tests")
        return env_token
    token_file = os.environ.get("ARGON_AGENT_TOKEN_FILE", "")
    if not token_file:
        raise RuntimeError("Configure ARGON_AGENT_TOKEN_FILE")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("This platform cannot securely open the agent token file")
    descriptor = None
    try:
        descriptor = os.open(token_file, os.O_RDONLY | nofollow)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError("Agent token file must be a regular file with mode 0600")
        with os.fdopen(descriptor, "r", encoding="ascii") as handle:
            descriptor = None
            value = handle.read(128).strip()
    except RuntimeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Agent token file is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not re.fullmatch(TOKEN_PATTERN, value):
        raise RuntimeError("Agent token file has an invalid credential")
    return value


class APIError(ToolError):
    pass


def create_server() -> MCPServer:
    api_origin, loopback = _origin()
    bearer = _token(loopback)
    mcp = MCPServer("Argon Recordings", version="1.0.0", log_level="ERROR")
    read_hint = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                                openWorldHint=False)
    derive_hint = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                  openWorldHint=False)
    cancel_hint = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True,
                                  openWorldHint=False)

    async def request_json(method: str, path: str, *, params: dict[str, Any] | None = None,
                           json_body: dict[str, Any] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
        timeout = httpx.Timeout(20.0, connect=5.0)
        async with httpx.AsyncClient(base_url=api_origin, trust_env=False,
                                     follow_redirects=False, verify=True, timeout=timeout) as client:
            try:
                async with client.stream(method, path, params=params, json=json_body, headers=headers) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        raise APIError(f"Argon API request failed (HTTP {response.status_code})")
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > MAX_JSON_BYTES:
                        raise APIError("Argon API response exceeds the 1 MiB limit")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunks) + len(chunk) > MAX_JSON_BYTES:
                            raise APIError("Argon API response exceeds the 1 MiB limit")
                        chunks.extend(chunk)
                    if "application/json" not in response.headers.get("content-type", "").lower():
                        raise APIError("Argon API returned an unexpected content type")
                    try:
                        return json.loads(chunks)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise APIError("Argon API returned invalid JSON") from exc
            except APIError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                raise APIError("Argon API request could not be completed") from exc

    async def request_image(path: str) -> Image:
        headers = {"Authorization": f"Bearer {bearer}", "Accept": "image/jpeg"}
        timeout = httpx.Timeout(20.0, connect=5.0)
        async with httpx.AsyncClient(base_url=api_origin, trust_env=False,
                                     follow_redirects=False, verify=True, timeout=timeout) as client:
            try:
                async with client.stream("GET", path, headers=headers) as response:
                    if response.status_code != 200:
                        raise APIError(f"Argon API image request failed (HTTP {response.status_code})")
                    if response.headers.get("content-type", "").split(";", 1)[0].lower() != "image/jpeg":
                        raise APIError("Argon API image response is not JPEG")
                    length = response.headers.get("content-length")
                    if length and int(length) > MAX_IMAGE_BYTES:
                        raise APIError("Image exceeds the 4 MiB limit")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                            raise APIError("Image exceeds the 4 MiB limit")
                        data.extend(chunk)
                    if not data.startswith(b"\xff\xd8\xff"):
                        raise APIError("Argon API image is not a JPEG file")
                    return Image(data=bytes(data), format="jpeg")
            except APIError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                raise APIError("Argon API image request could not be completed") from exc

    def page_params(cursor: str | None, limit: int) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return params

    @mcp.tool(annotations=read_hint)
    async def list_projects() -> dict[str, Any]:
        """List projects available to this Argon agent grant."""
        return await request_json("GET", "/api/agent/projects")

    @mcp.tool(annotations=read_hint)
    async def list_recordings(project_id: Annotated[str, Field(pattern=ID_PATTERN)],
                              cursor: Annotated[str | None, Field(max_length=2048)] = None,
                              limit: Annotated[int, Field(ge=1, le=40)] = 20) -> dict[str, Any]:
        """List recordings in an authorized project, with an opaque next-page cursor."""
        return await request_json("GET", f"/api/agent/projects/{project_id}/recordings",
                                  params=page_params(cursor, limit))

    @mcp.tool(annotations=read_hint)
    async def search_recordings(project_id: Annotated[str, Field(pattern=ID_PATTERN)],
                                q: Annotated[str, Field(min_length=1, max_length=300)],
                                mode: Literal["hybrid", "semantic", "keyword"] = "hybrid") -> list[Any]:
        """Search authorized recordings and transcript passages."""
        return await request_json("GET", "/api/agent/search",
                                  params={"project_id": project_id, "q": q, "mode": mode})

    @mcp.tool(annotations=read_hint)
    async def get_recording(recording_id: Annotated[str, Field(pattern=ID_PATTERN)]) -> dict[str, Any]:
        """Read recording metadata and its current source/evidence snapshot."""
        return await request_json("GET", f"/api/agent/recordings/{recording_id}")

    @mcp.tool(annotations=read_hint)
    async def get_transcript(recording_id: Annotated[str, Field(pattern=ID_PATTERN)],
                             cursor: Annotated[str | None, Field(max_length=2048)] = None,
                             limit: Annotated[int, Field(ge=1, le=40)] = 20,
                             include_words: bool = False) -> dict[str, Any]:
        """Read transcript segments with exact source times and optional word detail."""
        params = page_params(cursor, limit)
        params["include_words"] = str(include_words).lower()
        return await request_json("GET", f"/api/agent/recordings/{recording_id}/transcript", params=params)

    @mcp.tool(annotations=read_hint)
    async def get_speakers(recording_id: Annotated[str, Field(pattern=ID_PATTERN)],
                           cursor: Annotated[str | None, Field(max_length=2048)] = None,
                           limit: Annotated[int, Field(ge=1, le=40)] = 20) -> dict[str, Any]:
        """Read speaker identities, turns, passages, and manual overrides."""
        return await request_json("GET", f"/api/agent/recordings/{recording_id}/speakers",
                                  params=page_params(cursor, limit))

    @mcp.tool(annotations=read_hint)
    async def list_marks(recording_id: Annotated[str, Field(pattern=ID_PATTERN)],
                         cursor: Annotated[str | None, Field(max_length=2048)] = None,
                         limit: Annotated[int, Field(ge=1, le=40)] = 20) -> dict[str, Any]:
        """List saved source-time marks for a recording."""
        return await request_json("GET", f"/api/agent/recordings/{recording_id}/marks",
                                  params=page_params(cursor, limit))

    @mcp.tool(annotations=read_hint)
    async def list_visual_evidence(recording_id: Annotated[str, Field(pattern=ID_PATTERN)],
                                   cursor: Annotated[str | None, Field(max_length=2048)] = None,
                                   limit: Annotated[int, Field(ge=1, le=50)] = 20) -> dict[str, Any]:
        """List derived screenshot and clip evidence metadata for an authorized recording."""
        return await request_json("GET", f"/api/agent/recordings/{recording_id}/visual-evidence",
                                  params=page_params(cursor, limit))

    @mcp.tool(annotations=read_hint)
    async def get_image(recording_id: Annotated[str, Field(pattern=ID_PATTERN)],
                        evidence_id: Annotated[str, Field(pattern=r"^(?:asset:[a-f0-9]{32}|frame:frame-[0-9]{4}\.jpg)$")]) -> Image:
        """Fetch one authorized JPEG as native MCP image content."""
        return await request_image(
            f"/api/agent/recordings/{recording_id}/images/{quote(evidence_id, safe='')}"
        )

    @mcp.tool(annotations=derive_hint)
    async def request_visual_evidence(reference: SourceReference, recipe: VisualRecipe) -> dict[str, Any]:
        """Request an authorized bounded screenshot or short clip; returns an asynchronous job."""
        return await request_json(
            "POST", f"/api/agent/recordings/{reference.recording_id}/visual-jobs",
            json_body={"reference": reference.model_dump(), "recipe": recipe.model_dump()},
        )

    @mcp.tool(annotations=read_hint)
    async def get_job(recording_id: Annotated[str, Field(pattern=ID_PATTERN)],
                      job_id: Annotated[str, Field(pattern=ID_PATTERN)]) -> dict[str, Any]:
        """Check progress or result for a visual evidence job."""
        return await request_json("GET", f"/api/agent/recordings/{recording_id}/visual-jobs/{job_id}")

    @mcp.tool(annotations=cancel_hint)
    async def cancel_visual_job(recording_id: Annotated[str, Field(pattern=ID_PATTERN)],
                                job_id: Annotated[str, Field(pattern=ID_PATTERN)]) -> dict[str, Any]:
        """Cancel an active visual derivation job; the retained source remains unchanged."""
        return await request_json("POST", f"/api/agent/recordings/{recording_id}/visual-jobs/{job_id}/cancel")

    return mcp


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.CRITICAL + 1)
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
