from __future__ import annotations

import base64
import socket

from common.errors import SyncError
from config.proxy import ImapProxySettings


def http_connect_socket(
    host: str,
    port: int,
    proxy: ImapProxySettings,
    timeout: float | None,
) -> socket.socket:
    """Open a TCP tunnel to host:port through an HTTP CONNECT proxy."""
    sock = socket.create_connection((proxy.host, proxy.port), timeout)
    try:
        authority = f"{host}:{port}"
        lines = [
            f"CONNECT {authority} HTTP/1.1",
            f"Host: {authority}",
            "Proxy-Connection: Keep-Alive",
        ]
        if proxy.username is not None:
            password = proxy.password or ""
            token = base64.b64encode(
                f"{proxy.username}:{password}".encode("utf-8")
            ).decode("ascii")
            lines.append(f"Proxy-Authorization: Basic {token}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        sock.sendall(request)

        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 65536:
                raise SyncError(
                    f"邮件代理响应过大：{proxy.host}:{proxy.port}"
                )

        header = bytes(response).split(b"\r\n\r\n", 1)[0]
        status_line = header.splitlines()[0] if header.splitlines() else b""
        parts = status_line.split(None, 2)
        if len(parts) < 2 or parts[1] != b"200":
            reason = status_line.decode("iso-8859-1", "replace")
            raise SyncError(
                f"邮件代理 CONNECT {authority} via "
                f"{proxy.host}:{proxy.port} 失败：{reason}"
            )
        return sock
    except Exception:
        sock.close()
        raise
