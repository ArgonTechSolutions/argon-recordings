"""Real MCP SDK stdio client tests against a temporary fixed HTTP API fixture."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters


ROOT = Path(__file__).resolve().parents[2]
SERVER = Path(__file__).with_name("server.py")
RECORDING = "a" * 32
PROJECT = "b" * 32
JOB = "c" * 32
TOKEN = "rec_agent_" + "A" * 43
JPEG = b"\xff\xd8\xff" + b"fixture" + b"\xff\xd9"


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[tuple[str, str, dict]] = []

    def log_message(self, _format, *_args):
        return

    def _write(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        type(self).requests.append(("GET", parsed.path, query))
        assert self.headers.get("Authorization") == f"Bearer {TOKEN}"
        if parsed.path.endswith("/images/asset%3A" + "e" * 32):
            self._write(200, b"\xff\xd8\xff" + b"x" * (4 * 1024 * 1024 + 1), "image/jpeg")
        elif "/images/" in parsed.path:
            self._write(200, JPEG, "image/jpeg")
        elif parsed.path.endswith("/recordings/" + "f" * 32):
            self._write(404, b'{"detail":"internal sensitive fixture detail"}', "application/json")
        elif parsed.path == "/api/agent/search" and query.get("q") == ["redirect"]:
            self.send_response(302)
            self.send_header("Location", f"http://{self.headers.get('Host')}/redirect-target")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif parsed.path == "/api/agent/search" and query.get("q") == ["large"]:
            self._write(200, b"{" + b" " * (1024 * 1024), "application/json")
        elif parsed.path == "/api/agent/search":
            self._write(200, b'[{"recording_id":"' + RECORDING.encode() + b'","text":"example"}]',
                        "application/json")
        else:
            payload = {"items": [{"id": RECORDING}], "next_cursor": "next"}
            if parsed.path.endswith("/visual-jobs/" + JOB):
                payload = {"id": JOB, "state": "completed", "progress": 1.0}
            elif parsed.path.endswith("/transcript"):
                payload = {"items": [{"id": "segment", "text": "hello"}],
                           "next_cursor": None, "include_words": query.get("include_words") == ["true"]}
            self._write(200, json.dumps(payload).encode(), "application/json")

    def do_POST(self):
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else None
        type(self).requests.append(("POST", parsed.path, body or {}))
        assert self.headers.get("Authorization") == f"Bearer {TOKEN}"
        payload = {"id": JOB, "state": "queued", "progress": 0.0}
        self._write(200, json.dumps(payload).encode(), "application/json")


@pytest.fixture
def api_server():
    FixtureHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _client_env(api_origin: str):
    return {
        "PATH": os.environ.get("PATH", ""),
        "ARGON_RECORDINGS_API_ORIGIN": api_origin,
        "ARGON_AGENT_TOKEN": TOKEN,
        "ARGON_AGENT_ALLOW_ENV_TOKEN": "1",
    }


def test_published_sdk_stdio_calls_annotations_pagination_and_image(api_server):
    asyncio.run(_published_sdk_stdio_calls_annotations_pagination_and_image(api_server))


async def _published_sdk_stdio_calls_annotations_pagination_and_image(api_server):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        cwd=str(ROOT),
        env=_client_env(api_server),
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        by_name = {tool.name: tool for tool in tools.tools}
        assert set(by_name) == {
            "list_projects", "list_recordings", "search_recordings", "get_recording",
            "get_transcript", "get_speakers", "list_marks", "list_visual_evidence",
            "get_image", "request_visual_evidence", "get_job", "cancel_visual_job",
        }
        assert by_name["list_projects"].annotations.read_only_hint is True
        assert by_name["request_visual_evidence"].annotations.read_only_hint is False
        assert by_name["request_visual_evidence"].annotations.destructive_hint is False
        assert by_name["cancel_visual_job"].annotations.destructive_hint is True
        recipe_schema = by_name["request_visual_evidence"].input_schema
        serialized = json.dumps(recipe_schema)
        assert "source_sha256" in serialized and "max_width" in serialized
        assert "url" not in recipe_schema.get("properties", {})

        result = await client.call_tool("list_projects", {})
        assert result.is_error is not True
        assert result.structured_content == {"items": [{"id": RECORDING}], "next_cursor": "next"}
        page = await client.call_tool("list_recordings", {
            "project_id": PROJECT, "cursor": "opaque/with space", "limit": 40,
        })
        assert page.structured_content["next_cursor"] == "next"
        hits = await client.call_tool("search_recordings", {
            "project_id": PROJECT, "q": "hello & code", "mode": "keyword",
        })
        assert hits.structured_content == {"result": [{"recording_id": RECORDING, "text": "example"}]}
        await client.call_tool("get_recording", {"recording_id": RECORDING})
        transcript = await client.call_tool("get_transcript", {
            "recording_id": RECORDING, "cursor": "c+1", "include_words": True,
        })
        assert transcript.structured_content["include_words"] is True
        await client.call_tool("get_speakers", {"recording_id": RECORDING})
        await client.call_tool("list_marks", {"recording_id": RECORDING})
        await client.call_tool("list_visual_evidence", {"recording_id": RECORDING})

        image = await client.call_tool("get_image", {
            "recording_id": RECORDING, "evidence_id": f"asset:{'d' * 32}",
        })
        image_content = next(block for block in image.content if block.type == "image")
        assert image_content.mime_type == "image/jpeg"
        assert base64.b64decode(image_content.data) == JPEG

        reference = {
            "recording_id": RECORDING, "source_sha256": "e" * 64,
            "interval": {"start": 0, "end": 60},
            "versions": {"transcript": 1, "speakers": 2, "annotations": 3, "extraction": 4},
        }
        created = await client.call_tool("request_visual_evidence", {
            "reference": reference,
            "recipe": {"kind": "image", "start": 4, "end": 4},
        })
        assert created.structured_content["id"] == JOB
        await client.call_tool("get_job", {"recording_id": RECORDING, "job_id": JOB})
        await client.call_tool("cancel_visual_job", {"recording_id": RECORDING, "job_id": JOB})

        invalid = await client.call_tool("list_recordings", {"project_id": "../outside"})
        assert invalid.is_error is True

    requests = FixtureHandler.requests
    recorded = {(method, path): query for method, path, query in requests}
    assert recorded[("GET", f"/api/agent/projects/{PROJECT}/recordings")]["cursor"] == ["opaque/with space"]
    assert recorded[("GET", f"/api/agent/projects/{PROJECT}/recordings")]["limit"] == ["40"]
    assert recorded[("GET", "/api/agent/search")]["q"] == ["hello & code"]
    assert recorded[("GET", f"/api/agent/recordings/{RECORDING}/transcript")]["include_words"] == ["true"]
    assert any(path.endswith("/images/asset%3A" + "d" * 32) for _, path, _ in requests)
    post_jobs = [body for method, path, body in requests
                 if method == "POST" and path.endswith("/visual-jobs")]
    assert post_jobs[0]["reference"] == reference
    assert post_jobs[0]["recipe"]["start"] == 4
    assert not any(path == "/redirect-target" for _, path, _ in requests)


def test_http_failures_are_safe_and_response_limits_are_enforced(api_server):
    asyncio.run(_http_failures_are_safe_and_response_limits_are_enforced(api_server))


async def _http_failures_are_safe_and_response_limits_are_enforced(api_server):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        cwd=str(ROOT),
        env=_client_env(api_server),
    )
    async with Client(params) as client:
        missing = await client.call_tool("get_recording", {"recording_id": "f" * 32})
        assert missing.is_error is True
        assert "internal sensitive fixture detail" not in str(missing.content)
        oversized = await client.call_tool("search_recordings", {
            "project_id": PROJECT, "q": "large", "mode": "keyword",
        })
        assert oversized.is_error is True
        assert "1 MiB" in str(oversized.content)
        redirect = await client.call_tool("search_recordings", {
            "project_id": PROJECT, "q": "redirect", "mode": "keyword",
        })
        assert redirect.is_error is True
        large_image = await client.call_tool("get_image", {
            "recording_id": RECORDING, "evidence_id": f"asset:{'e' * 32}",
        })
        assert large_image.is_error is True
        assert "4 MiB" in str(large_image.content)


def test_origin_and_token_file_validation(tmp_path, monkeypatch):
    sys.path.insert(0, str(SERVER.parent))
    import server

    monkeypatch.setenv("ARGON_RECORDINGS_API_ORIGIN", "http://127.0.0.1:8080")
    assert server._origin() == ("http://127.0.0.1:8080", True)
    for invalid in ("http://example.com", "https://user:pass@example.com", "https://example.com/path",
                    "https://example.com/?q=x", "file:///tmp/data"):
        monkeypatch.setenv("ARGON_RECORDINGS_API_ORIGIN", invalid)
        with pytest.raises(RuntimeError):
            server._origin()

    token_path = tmp_path / "grant"
    token_path.write_text(TOKEN)
    token_path.chmod(0o600)
    monkeypatch.setenv("ARGON_AGENT_TOKEN_FILE", str(token_path))
    monkeypatch.delenv("ARGON_AGENT_TOKEN", raising=False)
    assert server._token(False) == TOKEN
    token_path.chmod(0o644)
    with pytest.raises(RuntimeError, match="mode 0600"):
        server._token(False)
    token_path.chmod(0o600)
    link = tmp_path / "grant-link"
    link.symlink_to(token_path)
    monkeypatch.setenv("ARGON_AGENT_TOKEN_FILE", str(link))
    with pytest.raises(RuntimeError, match="unavailable"):
        server._token(False)
    monkeypatch.setenv("ARGON_AGENT_TOKEN", TOKEN)
    monkeypatch.setenv("ARGON_AGENT_ALLOW_ENV_TOKEN", "1")
    with pytest.raises(RuntimeError, match="loopback tests"):
        server._token(False)
    assert server._token(True) == TOKEN
