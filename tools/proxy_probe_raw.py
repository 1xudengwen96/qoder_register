"""裸 socket 探测代理：区分 HTTP CONNECT / SOCKS5，绕过 requests 的封装。

    python tools/proxy_probe_raw.py --host gate.ipdeep.com --port 8082 ^
        --user 用户名 --pass 密码
"""

from __future__ import annotations

import argparse
import base64
import socket
import struct
import sys


def tcp(host: str, port: int, timeout: float = 8.0) -> str:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return "OK"
    except Exception as exc:
        return f"FAIL {type(exc).__name__}: {exc}"


def http_connect(host: str, port: int, user: str, pwd: str,
                 target: str = "api.ip.sb:443", timeout: float = 12.0) -> str:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except Exception as exc:
        return f"FAIL 连不上 {type(exc).__name__}: {exc}"
    s.settimeout(timeout)
    try:
        auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        req = (f"CONNECT {target} HTTP/1.1\r\n"
               f"Host: {target}\r\n"
               f"Proxy-Authorization: Basic {auth}\r\n"
               f"Proxy-Connection: keep-alive\r\n\r\n")
        s.sendall(req.encode())
        resp = s.recv(2048).decode("latin-1", "replace")
        return "第一行: " + resp.split("\r\n", 1)[0][:120]
    except Exception as exc:
        return f"FAIL {type(exc).__name__}: {exc}"
    finally:
        s.close()


def socks5(host: str, port: int, user: str, pwd: str,
           target: str = "api.ip.sb", tport: int = 443,
           timeout: float = 12.0) -> str:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except Exception as exc:
        return f"FAIL 连不上 {type(exc).__name__}: {exc}"
    s.settimeout(timeout)
    try:
        s.sendall(b"\x05\x02\x00\x02")                 # 支持 无认证/用户名密码
        head = s.recv(2)
        if len(head) < 2:
            return "FAIL 握手无响应"
        method = head[1]
        if method == 0x02:
            u, p = user.encode(), pwd.encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            rep = s.recv(2)
            if len(rep) < 2 or rep[1] != 0:
                return f"FAIL 认证失败 rep={rep!r}"
        elif method != 0x00:
            return f"FAIL 不支持的方法 0x{method:02x}"
        req = (b"\x05\x01\x00\x03" + bytes([len(target)]) + target.encode()
               + struct.pack(">H", tport))
        s.sendall(req)
        rep = s.recv(4)
        if len(rep) < 4 or rep[1] != 0:
            return f"FAIL CONNECT 被拒 rep={rep!r}"
        return f"OK SOCKS5 隧道已建立（method=0x{method:02x}）"
    except Exception as exc:
        return f"FAIL {type(exc).__name__}: {exc}"
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="gate.ipdeep.com")
    ap.add_argument("--port", type=int, action="append", default=None)
    ap.add_argument("--user", required=True)
    ap.add_argument("--pass", dest="pwd", required=True)
    args = ap.parse_args()

    ports = args.port or [8080, 8082]
    for port in ports:
        print(f"\n=== {args.host}:{port} ===")
        print(f"  TCP       {tcp(args.host, port)}")
        print(f"  HTTP      {http_connect(args.host, port, args.user, args.pwd)}")
        print(f"  SOCKS5    {socks5(args.host, port, args.user, args.pwd)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
