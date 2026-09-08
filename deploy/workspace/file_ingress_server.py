#!/usr/bin/env python3
"""Workspace-local MCP child for bounded ChatGPT file transfer.

The gateway and workspace proxy remain loopback-only. This child runs inside
the workspace service, where the reference composition has network egress and
read/write access only to the bounded workspace root.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import ipaddress
import mimetypes
import os
import re
import socket
import ssl
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from typing_extensions import NotRequired, TypedDict

MAX_FILE_BYTES = 100 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60.0
MAX_REDIRECTS = 3
STREAM_CHUNK_BYTES = 1024 * 1024
_EXPORT_RESOURCE_TEMPLATE = "haru-workspace-file://export/{token}"
_MAX_EXPORT_TOKEN_CHARS = 8192
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_OPENAI_AZURE_BLOB_HOST = re.compile(
    r"^oaisdmntpr[a-z0-9]{2,40}\.blob\.core\.windows\.net$"
)


class OpenAIFileRef(TypedDict):
    """File reference shape injected by ChatGPT for openai/fileParams."""

    download_url: str
    file_id: str
    mime_type: NotRequired[str]
    file_name: NotRequired[str]


def _trusted_download_url(raw: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError("file.download_url is missing")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise ValueError("file.download_url is invalid") from None
    if parts.scheme != "https":
        raise ValueError("file.download_url must use HTTPS")
    if parts.username is not None or parts.password is not None:
        raise ValueError("file.download_url must not contain credentials")
    if parts.fragment:
        raise ValueError("file.download_url must not contain a fragment")
    if port not in (None, 443):
        raise ValueError("file.download_url must use HTTPS port 443")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError("file.download_url host is missing")
    openai_content_host = host == "oaiusercontent.com" or host.endswith(
        ".oaiusercontent.com"
    )
    if not openai_content_host and not _OPENAI_AZURE_BLOB_HOST.fullmatch(host):
        raise ValueError("file.download_url host is not allowed")
    return raw


def _resolve_public_addresses(host: str) -> list[str]:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError("file.download_url host could not be resolved") from None
    if not answers:
        raise ValueError("file.download_url host could not be resolved")

    resolved: list[str] = []
    for answer in answers:
        raw_ip = answer[4][0]
        try:
            address = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise ValueError("file.download_url resolved to an invalid address") from None
        if not address.is_global:
            raise ValueError("file.download_url resolved to a non-public address")
        normalized = str(address)
        if normalized not in resolved:
            resolved.append(normalized)
    return resolved


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to one validated address while keeping host TLS/SNI."""

    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(
            host,
            port=443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_address = address

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _safe_destination(root: Path, destination: str) -> Path:
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("destination is required")
    relative = Path(destination)
    if relative.is_absolute():
        raise ValueError("destination must be relative to the workspace root")
    if ".." in relative.parts:
        raise ValueError("destination must not escape the workspace root")
    if relative.name in {"", ".", ".."}:
        raise ValueError("destination must name a file")

    workspace = root.resolve(strict=True)
    try:
        parent = (workspace / relative).parent.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError("destination parent directory does not exist") from None
    if not parent.is_relative_to(workspace):
        raise ValueError("destination must stay inside the workspace root")
    return parent / relative.name


def _safe_source(root: Path, source: str) -> Path:
    if not isinstance(source, str) or not source.strip():
        raise ValueError("path is required")
    relative = Path(source)
    if relative.is_absolute():
        raise ValueError("path must be relative to the workspace root")
    if ".." in relative.parts:
        raise ValueError("path must not escape the workspace root")
    if relative.name in {"", ".", ".."}:
        raise ValueError("path must name a file")

    workspace = root.resolve(strict=True)
    try:
        target = (workspace / relative).resolve(strict=True)
    except FileNotFoundError:
        raise ValueError("file does not exist") from None
    if not target.is_relative_to(workspace):
        raise ValueError("path must stay inside the workspace root")
    if not target.is_file():
        raise ValueError("path must name a regular file")
    return target


def prepare_workspace_file_export(path: str) -> dict[str, Any]:
    root = Path.cwd().resolve(strict=True)
    target = _safe_source(root, path)
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError("file exceeds the 100 MiB export limit")
    return {
        "path": target.relative_to(root).as_posix(),
        "name": target.name,
        "bytes": size,
        "mime_type": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
    }


def _decode_export_token(token: str) -> str:
    if not isinstance(token, str) or not token or len(token) > _MAX_EXPORT_TOKEN_CHARS:
        raise ValueError("invalid workspace export resource token")
    padding = "=" * (-len(token) % 4)
    try:
        raw = base64.b64decode(token + padding, altchars=b"-_", validate=True)
        path = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise ValueError("invalid workspace export resource token") from None
    if not path:
        raise ValueError("invalid workspace export resource token")
    return path


def read_workspace_export_resource(token: str) -> bytes:
    path = _decode_export_token(token)
    metadata = prepare_workspace_file_export(path)
    root = Path.cwd().resolve(strict=True)
    target = _safe_source(root, metadata["path"])
    with target.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file exceeds the 100 MiB export limit")
    return data


def _read_content_length(response: http.client.HTTPResponse) -> int | None:
    raw = response.getheader("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("file download returned an invalid Content-Length") from None
    if value < 0:
        raise ValueError("file download returned an invalid Content-Length")
    return value


def _open_https(url: str, timeout_seconds: float) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse]:
    parts = urlsplit(url)
    host = parts.hostname
    if host is None:
        raise ValueError("file.download_url host is missing")
    addresses = _resolve_public_addresses(host)
    target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    for address in addresses:
        connection = _PinnedHTTPSConnection(host, address, timeout_seconds)
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "User-Agent": "haru-workspace-file-ingress/1",
                },
            )
            return connection, connection.getresponse()
        except (OSError, ssl.SSLError, http.client.HTTPException):
            connection.close()
    raise ValueError("OpenAI file download failed") from None


def _download_to_temp(initial_url: str, parent: Path, name: str) -> tuple[Path, int, str]:
    url = _trusted_download_url(initial_url)
    temp_path: Path | None = None
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
    for hop in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("OpenAI file download timed out")
        connection, response = _open_https(url, remaining)
        try:
            if response.status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("OpenAI file redirect was missing a location")
                if hop == MAX_REDIRECTS:
                    raise ValueError("OpenAI file download exceeded the redirect limit")
                url = _trusted_download_url(urljoin(url, location))
                continue
            if response.status != 200:
                raise ValueError(f"OpenAI file download failed with HTTP status {response.status}")
            encoding = (response.getheader("Content-Encoding") or "identity").lower()
            if encoding not in {"", "identity"}:
                raise ValueError("OpenAI file download returned an unsupported content encoding")
            declared = _read_content_length(response)
            if declared is not None and declared > MAX_FILE_BYTES:
                raise ValueError("file exceeds the 100 MiB import limit")

            fd, raw_temp = tempfile.mkstemp(prefix=f".{name}.", suffix=".part", dir=parent)
            temp_path = Path(raw_temp)
            os.fchmod(fd, 0o600)
            size = 0
            digest = hashlib.sha256()
            try:
                with os.fdopen(fd, "wb", closefd=True) as handle:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ValueError("OpenAI file download timed out")
                        sock = getattr(connection, "sock", None)
                        if sock is not None:
                            sock.settimeout(remaining)
                        chunk = response.read(STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise ValueError("file exceeds the 100 MiB import limit")
                        handle.write(chunk)
                        digest.update(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                temp_path.unlink(missing_ok=True)
                temp_path = None
                raise
            if declared is not None and size != declared:
                temp_path.unlink(missing_ok=True)
                temp_path = None
                raise ValueError("OpenAI file download size did not match Content-Length")
            return temp_path, size, digest.hexdigest()
        except (OSError, http.client.HTTPException):
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise ValueError("OpenAI file download failed") from None
        finally:
            response.close()
            connection.close()
    raise ValueError("OpenAI file download exceeded the redirect limit")


def import_chatgpt_file(
    file: OpenAIFileRef,
    destination: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    if not isinstance(file, dict):
        raise ValueError("file must be a ChatGPT-provided file object")
    file_id = file.get("file_id")
    if not isinstance(file_id, str) or not file_id or len(file_id) > 1024:
        raise ValueError("file.file_id is missing or invalid")
    download_url = file.get("download_url")
    if not isinstance(download_url, str):
        raise ValueError("file.download_url is missing")

    root = Path.cwd()
    target = _safe_destination(root, destination)
    if not overwrite and (target.exists() or target.is_symlink()):
        raise ValueError("destination already exists")

    temp_path, size, digest = _download_to_temp(download_url, target.parent, target.name)
    try:
        if overwrite:
            os.replace(temp_path, target)
        else:
            try:
                os.link(temp_path, target, follow_symlinks=False)
            except FileExistsError:
                raise ValueError("destination already exists") from None
            finally:
                temp_path.unlink(missing_ok=True)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    payload: dict[str, Any] = {
        "path": target.relative_to(root.resolve(strict=True)).as_posix(),
        "bytes": size,
        "sha256": digest,
        "file_id": file_id,
    }
    mime_type = file.get("mime_type")
    file_name = file.get("file_name")
    if isinstance(mime_type, str) and mime_type:
        payload["mime_type"] = mime_type
    if isinstance(file_name, str) and file_name:
        payload["file_name"] = file_name
    return payload


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(name="haru-workspace-file-ingress", log_level="WARNING")
    server.tool(
        name="import_chatgpt_file",
        description=(
            "Import a ChatGPT-provided file reference into the current workspace. "
            "The file must be supplied by a trusted OpenAI host and the destination "
            "must be a relative path whose parent directory already exists."
        ),
    )(import_chatgpt_file)
    server.tool(
        name="prepare_workspace_file_export",
        description=(
            "Validate a workspace-relative regular file for export and return only "
            "its bounded metadata."
        ),
    )(prepare_workspace_file_export)
    server.resource(
        _EXPORT_RESOURCE_TEMPLATE,
        name="workspace-file-export",
        description="Bounded workspace file bytes for the Haru gateway.",
        mime_type="application/octet-stream",
    )(read_workspace_export_resource)
    return server


def main() -> int:
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())