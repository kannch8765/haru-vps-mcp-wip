from __future__ import annotations

import base64

import pytest
from mcp import types

from haru_mcp import server as server_module
from haru_mcp.server import _decode_export_token, _encode_export_path, build_server
from haru_mcp.settings import load_settings


def _result(payload: dict[str, object]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent=payload,
        isError=False,
    )


def test_export_resource_token_round_trips_unicode_path() -> None:
    path = "artifacts/晴/結果.bin"
    assert _decode_export_token(_encode_export_path(path)) == path


@pytest.mark.parametrize("token", ["", "*", "====", "a" * 9000])
def test_export_resource_token_rejects_invalid_values(token: str) -> None:
    with pytest.raises(ValueError, match="invalid workspace export resource token"):
        _decode_export_token(token)


async def test_workspace_export_resource_link_round_trips_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"hello from workspace\x00\xff"
    metadata = {
        "path": "artifacts/hello.bin",
        "name": "hello.bin",
        "bytes": len(body),
        "mime_type": "application/octet-stream",
    }

    async def fake_prepare(settings, path):
        assert path == "artifacts/hello.bin"
        return _result(metadata)

    async def fake_read(settings, token):
        assert _decode_export_token(token) == "artifacts/hello.bin"
        uri = f"haru-workspace-file://export/{token}"
        return types.ReadResourceResult(
            contents=[
                types.BlobResourceContents(
                    uri=uri,
                    mimeType="application/octet-stream",
                    blob=base64.b64encode(body).decode("ascii"),
                )
            ]
        )

    monkeypatch.setattr(server_module, "workspace_prepare_file_export", fake_prepare)
    monkeypatch.setattr(server_module, "workspace_read_file_export_resource", fake_read)
    server = build_server(load_settings(env={}))

    called = await server.call_tool("workspace_export_file", {"path": "artifacts/hello.bin"})
    assert called.isError is False
    assert len(called.content) == 1
    link = called.content[0]
    assert isinstance(link, types.ResourceLink)
    assert link.name == "hello.bin"
    assert link.mimeType == "application/octet-stream"
    assert link.size == len(body)

    resource = await server.read_resource(str(link.uri))
    assert len(resource) == 1
    assert resource[0].content == body
    assert resource[0].mime_type == "application/octet-stream"


async def test_workspace_export_tool_preserves_child_error(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = types.CallToolResult(
        content=[types.TextContent(type="text", text="file does not exist")],
        isError=True,
    )

    async def fake_prepare(settings, path):
        return failure

    monkeypatch.setattr(server_module, "workspace_prepare_file_export", fake_prepare)
    server = build_server(load_settings(env={}))
    called = await server.call_tool("workspace_export_file", {"path": "missing.bin"})
    assert called.isError is True
    assert called.content[0].text == "file does not exist"
