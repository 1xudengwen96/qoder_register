"""单条代理诊断：打印门关返回的原始内容（判断限流 / 会话无效 / 认证问题）。"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys

from app.net.proxy import parse_line


def fetch(px, url_host: str = "api.ip.sb", path: str = "/geoip",
          timeout: float = 20.0) -> None:
    raw = px.raw
    conn = socket.create_connection((px.host, int(px.port or 8080)),
                                    timeout=timeout)
    conn.settimeout(timeout)
    import base64
    auth = base64.b64encode(f"{px.username}:{px.password}".encode()).decode()
    req = (f"CONNECT {url_host}:443 HTTP/1.1\r\n"
           f"Host: {url_host}:443\r\n"
           f"Proxy-Authorization: Basic {auth}\r\n\r\n")
    conn.sendall(req.encode())
    head = conn.recv(4096).decode("latin-1", "replace")
    print("  CONNECT:", head.split("\r\n", 1)[0][:120])
    if " 200" not in head.split("\r\n", 1)[0]:
        print("  拒绝，原文:", head[:300].replace("\r\n", " | "))
        conn.close()
        return
    ctx = ssl.create_default_context()
    with ctx.wrap_socket(conn, server_hostname=url_host) as s:
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: {url_host}\r\n"
                   f"User-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n").encode())
        buf = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 8192:
                    break
        except Exception as exc:
            print("  读超时:", type(exc).__name__)
        text = buf.decode("utf-8", "replace")
        print("  HTTP:", text.split("\r\n", 1)[0][:100])
        body = text.split("\r\n\r\n", 1)[-1]
        print("  正文:", body[:300].replace("\n", " ").replace("\r", ""))
    print("  原始:", raw[:90])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", required=True, help="host:port:user:pass")
    args = ap.parse_args()
    px = parse_line(args.proxy)
    if px is None:
        print("解析失败")
        return 2
    print(f"host={px.host} port={px.port} user={px.username}")
    fetch(px)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
