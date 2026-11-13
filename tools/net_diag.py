"""网络连通性诊断：DNS / 直连 993 / 经 HTTP 代理 CONNECT 打 993。

    python tools/net_diag.py
    python tools/net_diag.py --host outlook.office365.com --port 993
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402

HOSTS = ["outlook.office365.com", "login.microsoftonline.com",
         "imap.gmail.com", "qoder.com"]


def dns(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 993, proto=socket.IPPROTO_TCP)
        return sorted({i[4][0] for i in infos})
    except Exception as exc:
        return [f"ERR {type(exc).__name__}: {exc}"]


def tcp(host: str, port: int, timeout: float = 8.0) -> str:
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        peer = s.getpeername()
        s.close()
        return f"OK {peer} {time.time() - t0:.1f}s"
    except Exception as exc:
        return f"FAIL {type(exc).__name__}: {exc} {time.time() - t0:.1f}s"


def tls(host: str, port: int, timeout: float = 10.0) -> str:
    ctx = ssl.create_default_context()
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                return f"OK {s.version()} {time.time() - t0:.1f}s"
    except Exception as exc:
        return f"FAIL {type(exc).__name__}: {exc} {time.time() - t0:.1f}s"


def via_proxy(proxy: str, host: str, port: int, timeout: float = 12.0,
              target_ip: str = "") -> str:
    """用 HTTP CONNECT 打隧道（Clash 的混合端口支持）。

    ``target_ip`` 给了就把 IPv4 直接写进 CONNECT 目标，避免代理端又走 IPv6。
    """
    if not proxy:
        return "SKIP 没有代理"
    hp = proxy.replace("http://", "").replace("https://", "")
    phost, pport = hp.rsplit(":", 1)
    target = f"{target_ip or host}:{port}"
    t0 = time.time()
    try:
        raw = socket.create_connection((phost, int(pport)), timeout=timeout)
    except Exception as exc:
        return f"FAIL 连代理 {type(exc).__name__}: {exc}"
    try:
        req = (f"CONNECT {target} HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n\r\n")
        raw.sendall(req.encode())
        raw.settimeout(timeout)
        resp = raw.recv(4096).decode("latin-1", "replace")
        first = resp.split("\r\n", 1)[0]
        if " 200" not in first:
            raw.close()
            return f"FAIL 隧道被拒: {first}"
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            info = f"OK 隧道+TLS({target}) {s.version()} {time.time() - t0:.1f}s"
        return info
    except Exception as exc:
        try:
            raw.close()
        except Exception:
            pass
        return f"FAIL 隧道后握手({target}) {type(exc).__name__}: {exc}"


def ipv4_list(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 993, socket.AF_INET,
                                   socket.SOCK_STREAM)
        return sorted({i[4][0] for i in infos})
    except Exception as exc:
        return [f"ERR {type(exc).__name__}: {exc}"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int, default=993)
    ap.add_argument("--proxy", default=secrets.get("proxy.local",
                                                   "http://127.0.0.1:7897"))
    args = ap.parse_args()

    hosts = [args.host] if args.host else HOSTS
    for h in hosts:
        print(f"\n=== {h}:{args.port} ===")
        print(f"  DNS     {dns(h)}")
        print(f"  直连    {tcp(h, args.port)}")
        if args.port in (443, 993, 995, 465, 587):
            print(f"  直连TLS {tls(h, args.port)}")
        ips = ipv4_list(h)
        print(f"  IPv4    {ips}")
        for ip in ips[:2]:
            print(f"    {ip} TCP  {tcp(ip, args.port)}")
            print(f"    {ip} TLS  {tls(ip, args.port)}")
        print(f"  代理隧道(域名) {via_proxy(args.proxy, h, args.port)}")
        for ip in ips[:2]:
            print(f"  代理隧道({ip}) "
                  f"{via_proxy(args.proxy, h, args.port, target_ip=ip)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
