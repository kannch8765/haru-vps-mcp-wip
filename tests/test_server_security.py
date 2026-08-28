from haru_mcp.server import _transport_security, build_server
from haru_mcp.settings import load_settings


def test_transport_allowlist_is_loopback_only_by_default():
    security = _transport_security(load_settings(env={}))
    assert security.enable_dns_rebinding_protection is True
    assert "127.0.0.1:8765" in security.allowed_hosts
    assert security.allowed_origins == []


def test_configured_public_origin_only_extends_transport_allowlist():
    cfg = load_settings(env={
        "HARU_MCP_PUBLIC_HOST": "mcp.example.com",
        "HARU_MCP_PUBLIC_ORIGIN": "https://mcp.example.com",
    })
    security = _transport_security(cfg)
    assert "mcp.example.com" in security.allowed_hosts
    assert security.allowed_origins == ["https://mcp.example.com"]
    assert cfg.host == "127.0.0.1"


async def test_chatgpt_file_import_advertises_host_file_contract():
    listed = await build_server(load_settings(env={})).list_tools()
    tool = next(item for item in listed if item.name == "workspace_import_chatgpt_file")
    assert tool.meta == {"openai/fileParams": ["file"]}
    schema = tool.inputSchema
    assert set(schema["required"]) == {"file", "destination"}
    file_schema = schema["properties"]["file"]
    if "$ref" in file_schema:
        definition = file_schema["$ref"].split("/")[-1]
        file_schema = schema["$defs"][definition]
    assert set(file_schema["required"]) == {"download_url", "file_id"}
    assert set(file_schema["properties"]) == {
        "download_url",
        "file_id",
        "mime_type",
        "file_name",
    }


async def test_workspace_file_export_is_exposed_as_resource_link_tool():
    listed = await build_server(load_settings(env={})).list_tools()
    tool = next(item for item in listed if item.name == "workspace_export_file")
    assert set(tool.inputSchema["required"]) == {"path"}
    templates = await build_server(load_settings(env={})).list_resource_templates()
    assert any(str(item.uriTemplate) == "haru-workspace://export/{token}" for item in templates)
