from __future__ import annotations

import importlib.util
import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "workspace" / "file_ingress_server.py"
SPEC = importlib.util.spec_from_file_location("haru_workspace_file_ingress", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ingress = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ingress
SPEC.loader.exec_module(ingress)


@pytest.mark.parametrize(
    "url",
    [
        "https://oaiusercontent.com/file.bin?sig=abc",
        "https://files.oaiusercontent.com/file.bin?sig=abc",
        "https://region.files.oaiusercontent.com/file.bin?sig=abc",
        "https://oaisdmntprwus01.blob.core.windows.net/container/file?sig=abc",
    ],
)
def test_trusted_download_url_accepts_openai_storage(url: str) -> None:
    assert ingress._trusted_download_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://files.oaiusercontent.com/file.bin",
        "https://user:pass@files.oaiusercontent.com/file.bin",
        "https://files.oaiusercontent.com:444/file.bin",
        "https://files.oaiusercontent.com/file.bin#fragment",
        "https://evil.example/file.bin",
        "https://files.oaiusercontent.com.evil.example/file.bin",
        "https://random.blob.core.windows.net/file.bin",
        "https://oaisdmntprwus01.blob.core.windows.net.evil.example/file.bin",
    ],
)
def test_trusted_download_url_rejects_non_openai_or_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        ingress._trusted_download_url(url)


def test_public_dns_guard_rejects_private_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ingress.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        ingress._resolve_public_addresses("files.oaiusercontent.com")


class _FakeConnection:
    def close(self) -> None:
        return None


class _FakeResponse:
    def __init__(self, *, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = status
        self._body = body
        self._offset = 0
        self._headers = headers or {}

    def getheader(self, name: str):
        return self._headers.get(name)

    def read(self, amount: int = -1) -> bytes:
        if self._offset >= len(self._body):
            return b""
        if amount < 0:
            amount = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        return None


def test_open_https_pins_validated_address(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    response = _FakeResponse(status=200)

    class _FakePinnedConnection:
        def __init__(self, host: str, address: str, timeout: float):
            captured.update(host=host, address=address, timeout=timeout)

        def request(self, method: str, target: str, headers: dict[str, str]) -> None:
            captured.update(method=method, target=target, headers=headers)

        def getresponse(self):
            return response

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        ingress,
        "_resolve_public_addresses",
        lambda host: ["203.0.113.10"],
    )
    monkeypatch.setattr(ingress, "_PinnedHTTPSConnection", _FakePinnedConnection)

    connection, got = ingress._open_https(
        "https://files.oaiusercontent.com/path/file.bin?sig=secret",
        12.5,
    )
    assert connection is not None
    assert got is response
    assert captured["host"] == "files.oaiusercontent.com"
    assert captured["address"] == "203.0.113.10"
    assert captured["timeout"] == 12.5
    assert captured["method"] == "GET"
    assert captured["target"] == "/path/file.bin?sig=secret"


def test_destination_must_remain_under_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inbox").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    assert ingress._safe_destination(workspace, "inbox/file.bin") == workspace / "inbox" / "file.bin"
    for bad in ("/tmp/file.bin", "../file.bin", "escape/file.bin", "missing/file.bin"):
        with pytest.raises(ValueError):
            ingress._safe_destination(workspace, bad)



def test_export_source_must_remain_under_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out").mkdir()
    (workspace / "out" / "file.bin").write_bytes(b"ok")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"secret")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    assert ingress._safe_source(workspace, "out/file.bin") == workspace / "out" / "file.bin"
    for bad in ("/tmp/file.bin", "../file.bin", "escape/secret.bin", "missing.bin", "out"):
        with pytest.raises(ValueError):
            ingress._safe_source(workspace, bad)


def test_prepare_workspace_file_export_returns_bounded_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "report.txt"
    target.write_bytes(b"hello")
    monkeypatch.chdir(workspace)

    result = ingress.prepare_workspace_file_export("report.txt")
    assert result == {
        "path": "report.txt",
        "name": "report.txt",
        "bytes": 5,
        "mime_type": "text/plain",
    }


def test_read_workspace_file_export_preserves_binary_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    body = b"binary\x00payload\xff"
    (workspace / "artifact.bin").write_bytes(body)
    monkeypatch.chdir(workspace)

    token = __import__("base64").urlsafe_b64encode(b"artifact.bin").decode("ascii").rstrip("=")
    assert ingress.read_workspace_export_resource(token) == body


def test_workspace_file_export_enforces_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "large.bin").write_bytes(b"12345")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(ingress, "MAX_FILE_BYTES", 4)

    with pytest.raises(ValueError, match="export limit"):
        ingress.prepare_workspace_file_export("large.bin")
    token = __import__("base64").urlsafe_b64encode(b"large.bin").decode("ascii").rstrip("=")
    with pytest.raises(ValueError, match="export limit"):
        ingress.read_workspace_export_resource(token)

def test_download_streams_to_temp_and_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"binary\x00payload\xff"
    response = _FakeResponse(
        status=200,
        body=body,
        headers={"Content-Length": str(len(body)), "Content-Encoding": "identity"},
    )
    monkeypatch.setattr(ingress, "_open_https", lambda url, timeout_seconds: (_FakeConnection(), response))

    temp, size, digest = ingress._download_to_temp(
        "https://files.oaiusercontent.com/file.bin?sig=secret",
        tmp_path,
        "file.bin",
    )
    assert temp.read_bytes() == body
    assert size == len(body)
    assert digest == __import__("hashlib").sha256(body).hexdigest()
    assert temp.stat().st_mode & 0o777 == 0o600
    temp.unlink()


def test_download_preserves_empty_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(
        status=200,
        body=b"",
        headers={"Content-Length": "0"},
    )
    monkeypatch.setattr(
        ingress,
        "_open_https",
        lambda url, timeout_seconds: (_FakeConnection(), response),
    )

    temp, size, digest = ingress._download_to_temp(
        "https://files.oaiusercontent.com/empty.bin?sig=secret",
        tmp_path,
        "empty.bin",
    )
    assert temp.read_bytes() == b""
    assert size == 0
    assert digest == __import__("hashlib").sha256(b"").hexdigest()
    temp.unlink()


def test_redirect_target_is_revalidated_before_second_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_open(url: str, timeout_seconds: float):
        calls.append(url)
        return _FakeConnection(), _FakeResponse(
            status=302,
            headers={"Location": "https://evil.example/stolen"},
        )

    monkeypatch.setattr(ingress, "_open_https", fake_open)
    with pytest.raises(ValueError, match="host is not allowed"):
        ingress._download_to_temp(
            "https://files.oaiusercontent.com/start?sig=secret",
            tmp_path,
            "file.bin",
        )
    assert len(calls) == 1


def test_streaming_limit_removes_partial_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingress, "MAX_FILE_BYTES", 4)
    response = _FakeResponse(status=200, body=b"12345")
    monkeypatch.setattr(ingress, "_open_https", lambda url, timeout_seconds: (_FakeConnection(), response))

    with pytest.raises(ValueError, match="import limit"):
        ingress._download_to_temp(
            "https://files.oaiusercontent.com/file.bin?sig=secret",
            tmp_path,
            "file.bin",
        )
    assert list(tmp_path.iterdir()) == []


def test_import_is_atomic_and_does_not_return_signed_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    inbox = workspace / "inbox"
    inbox.mkdir(parents=True)
    monkeypatch.chdir(workspace)

    body = b"hello from chatgpt file"

    def fake_download(url: str, parent: Path, name: str):
        path = parent / ".fake.part"
        path.write_bytes(body)
        os.chmod(path, 0o600)
        return path, len(body), __import__("hashlib").sha256(body).hexdigest()

    monkeypatch.setattr(ingress, "_download_to_temp", fake_download)
    result = ingress.import_chatgpt_file(
        {
            "download_url": "https://files.oaiusercontent.com/file.bin?sig=must-not-return",
            "file_id": "file_123",
            "mime_type": "application/octet-stream",
            "file_name": "original.bin",
        },
        "inbox/copied.bin",
    )

    destination = inbox / "copied.bin"
    assert destination.read_bytes() == body
    assert destination.stat().st_mode & 0o777 == 0o600
    assert result["path"] == "inbox/copied.bin"
    assert result["bytes"] == len(body)
    assert result["file_id"] == "file_123"
    assert "download_url" not in result
    assert "must-not-return" not in repr(result)


def test_import_refuses_overwrite_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    inbox = workspace / "inbox"
    inbox.mkdir(parents=True)
    destination = inbox / "existing.bin"
    destination.write_bytes(b"keep")
    monkeypatch.chdir(workspace)

    def should_not_download(*args, **kwargs):
        raise AssertionError("download should not start when destination exists")

    monkeypatch.setattr(ingress, "_download_to_temp", should_not_download)
    with pytest.raises(ValueError, match="already exists"):
        ingress.import_chatgpt_file(
            {
                "download_url": "https://files.oaiusercontent.com/file.bin?sig=secret",
                "file_id": "file_123",
            },
            "inbox/existing.bin",
        )
    assert destination.read_bytes() == b"keep"


def test_child_mcp_tool_schema_matches_openai_file_contract() -> None:
    pytest.importorskip("mcp")
    import asyncio

    server = ingress.build_server()
    listed = asyncio.run(server.list_tools())
    assert {tool.name for tool in listed} == {
        "import_chatgpt_file",
        "prepare_workspace_file_export",
    }
    templates = asyncio.run(server.list_resource_templates())
    assert {str(item.uriTemplate) for item in templates} == {
        "haru-workspace-file://export/{token}"
    }
    tool = next(tool for tool in listed if tool.name == "import_chatgpt_file")
    schema = tool.inputSchema
    file_schema = schema["properties"]["file"]
    if "$ref" in file_schema:
        definition = file_schema["$ref"].split("/")[-1]
        file_schema = schema["$defs"][definition]
    assert set(file_schema["properties"]) == {
        "download_url",
        "file_id",
        "mime_type",
        "file_name",
    }
    assert set(file_schema["required"]) == {"download_url", "file_id"}
    assert file_schema["properties"]["mime_type"]["type"] == "string"
    assert file_schema["properties"]["file_name"]["type"] == "string"