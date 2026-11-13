"""进程内 TCP 转发（线程版）。

为什么是进程内
--------------
比特浏览器的 IP 检测在代理主机为 ``127.0.0.1`` 时会绕过代理走直连，
导致环境 ``lastIp`` 永远是本机出口（实测 CN），时区/地理跟随失效。
``netsh portproxy`` 需要管理员权限；独立进程转发在这种受限环境里会被
回收。所以做成**当前进程内的常驻线程**：谁需要谁起，进程活多久它就活多久。

用法
----
    from app.net.proxy_relay import LocalRelay

    relay = LocalRelay(target="127.0.0.1:7897", listen_host="0.0.0.0",
                       listen_port=7898).start()
    print(relay.url("192.168.1.41"))     # http://192.168.1.41:7898
    ...
    relay.stop()

纯字节管道，CONNECT / 明文 HTTP 都透明转发，不做协议解析。
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field


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


@dataclass
class LocalRelay:
    target: str = "127.0.0.1:7897"
    listen_host: str = "0.0.0.0"
    listen_port: int = 7898
    _sock: socket.socket | None = None
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    connections: int = 0

    def start(self, timeout: float = 5.0) -> "LocalRelay":
        host, port = self.target.rsplit(":", 1)
        self._upstream = (host, int(port))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.listen_host, self.listen_port))
        s.listen(64)
        s.settimeout(0.5)
        self._sock = s
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="proxy-relay")
        self._thread.start()
        # 自检：确认端口真的可连
        try:
            probe = socket.create_connection(("127.0.0.1", self.listen_port),
                                             timeout=timeout)
            probe.close()
        except OSError as exc:
            raise RuntimeError(f"转发端口自检失败: {exc}") from exc
        return self

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,),
                             daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(self._upstream, timeout=15)
        except OSError:
            client.close()
            return
        self.connections += 1
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

    def url(self, host: str) -> str:
        return f"http://{host}:{self.listen_port}"

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


def lan_ip() -> str:
    """取本机对外网卡 IP（不真的发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
