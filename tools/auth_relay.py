"""带鉴权头注入的本地 HTTP 代理中转。

客户端（Electron/Chromium）的 ``--proxy-server`` 不支持 URL 内嵌用户名密码，
而 ipdeep 上游是 Basic 鉴权代理。本工具监听本地端口，把客户端的 CONNECT
请求向上游转发时注入 ``Proxy-Authorization: Basic ...``，明文 HTTP 请求
同样注入后原样转发，之后就是纯字节管道。

用法::

    # 常驻中转（127.0.0.1:7899 -> gate.ipdeep.com:8080）
    python tools/auth_relay.py

    # 只测出口 IP（不常驻）
    python tools/auth_relay.py --probe
"""
from __future__ import annotations

import argparse
import base64
import socket
import threading
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config, get as cfg_get  # noqa: E402


def _credentials() -> tuple[str, str, str]:
    cfg = load_config()
    host = get(cfg, "ipdeep.gate_host", "gate.ipdeep.com")
    port = int(get(cfg, "ipdeep.gate_port", 8080))
    user = get(cfg, "ipdeep.gate_user", "")
    pwd = get(cfg, "ipdeep.gate_pass", "")
    return f"{host}:{port}", user, pwd


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class AuthRelay:
    def __init__(self, listen_host: str, listen_port: int,
                 upstream: str, auth_header: str):
        self.upstream_host, self.upstream_port = upstream.rsplit(":", 1)
        self.upstream_port = int(self.upstream_port)
        self.auth = auth_header
        self.listen_host, self.listen_port = listen_host, listen_port
        self._sock: socket.socket | None = None

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.listen_host, self.listen_port))
        s.listen(64)
        self._sock = s
        threading.Thread(target=self._serve, daemon=True,
                         name="auth-relay").start()

    def _serve(self) -> None:
        assert self._sock is not None
        while True:
            try:
                client, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,),
                             daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(65536)
                if not chunk:
                    client.close()
                    return
                head += chunk
                if len(head) > 65536:
                    client.close()
                    return
        except OSError:
            client.close()
            return

        try:
            upstream = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=20)
        except OSError:
            client.close()
            return

        line1 = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if line1.upper().startswith("CONNECT "):
            req = head.split(b"\r\n\r\n", 1)[0] + b"\r\n"
            # 去掉客户端自带的（无凭证的）Proxy-Authorization，注入正确的
            keep = [l for l in req.split(b"\r\n")[1:]
                    if not l.lower().startswith(b"proxy-authorization")
                    and not l.lower().startswith(b"proxy-connection")]
            out = b"\r\n".join(
                [req.split(b"\r\n", 1)[0], b"Proxy-Authorization: " + self.auth,
                 b"Proxy-Connection: keep-alive"] + keep) + b"\r\n\r\n"
            upstream.sendall(out)
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                resp += chunk
            ok = resp.startswith(b"HTTP/1.") and b" 2" in resp.split(b"\r\n", 1)[0]
            client.sendall(
                resp if ok else
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            if not ok:
                client.close()
                upstream.close()
                return
            rest = resp.split(b"\r\n\r\n", 1)[1]
            if rest:
                client.sendall(rest)
        else:
            # 明文 HTTP（绝对 URI）：注入鉴权头后原样转发
            out = head
            if b"Proxy-Authorization" not in head:
                first, rest_req = head.split(b"\r\n", 1)
                out = (first + b"\r\nProxy-Authorization: "
                       + self.auth + b"\r\n" + rest_req)
            upstream.sendall(out)

        t1 = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
        t2 = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        for s in (client, upstream):
            try:
                s.close()
            except OSError:
                pass


def probe(upstream: str, user: str, pwd: str) -> int:
    """通过上游代理测出口 IP。"""
    import requests
    proxy_url = f"http://{user}:{pwd}@{upstream}"
    r = requests.get("https://api.ipify.org?format=json",
                     proxies={"http": proxy_url, "https": proxy_url}, timeout=25)
    print(f"ipdeep 出口 IP: {r.json()['ip']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:7899")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()

    upstream, user, pwd = _credentials()
    if args.probe:
        return probe(upstream, user, pwd)

    auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    host, port = args.listen.rsplit(":", 1)
    relay = AuthRelay(host, int(port), upstream, auth.encode())
    relay.start()
    print(f"auth-relay {args.listen} -> {upstream} (Basic 鉴权已注入)")
    import time
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
