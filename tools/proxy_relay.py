"""用户态 TCP 转发：把本地代理暴露成一个「非 127.0.0.1」的地址。

背景
----
比特浏览器的「IP 检测」在代理主机是 ``127.0.0.1`` 时会绕过代理走直连，
于是环境记录里的 ``lastIp`` 永远是本机 CN 出口，时区/地理跟随也就失效
（实测 lastIp=240e:... 而浏览器页面里出口是 SG）。

``netsh portproxy`` 需要管理员权限，被拒；本机 Clash 只监听 127.0.0.1:7897。
所以用一个纯用户态 TCP 转发把 7897 桥到局域网地址上：

    python tools/proxy_relay.py --listen 0.0.0.0:7898 --target 127.0.0.1:7897

之后比特浏览器环境的代理填 ``http://<本机网卡IP>:7898``（例如
``http://192.168.1.41:7898``），它的 IP 检测就会真的走代理出去。

实现说明：纯字节管道，CONNECT / 明文 HTTP 都透明转发，不做协议解析，
所以 HTTPS 隧道、Socks 封装都不受影响。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

BYTES = 65536


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                tag: str) -> None:
    try:
        while True:
            data = await reader.read(BYTES)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle(client_r: asyncio.StreamReader,
                  client_w: asyncio.StreamWriter, target_host: str,
                  target_port: int) -> None:
    peer = client_w.get_extra_info("peername")
    try:
        up_r, up_w = await asyncio.open_connection(target_host, target_port)
    except Exception as exc:
        print(f"[relay] 上游连接失败 {target_host}:{target_port} "
              f"{type(exc).__name__}", flush=True)
        client_w.close()
        return
    await asyncio.gather(
        _pipe(client_r, up_w, "up"),
        _pipe(up_r, client_w, "down"),
    )
    print(f"[relay] 连接结束 {peer}", flush=True)


async def serve(listen_host: str, listen_port: int,
                target_host: str, target_port: int) -> None:
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, target_host, target_port),
        listen_host, listen_port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    print(f"[relay] 监听 {addrs} -> {target_host}:{target_port}", flush=True)
    print(f"[relay] 启动时间 {time.strftime('%H:%M:%S')}", flush=True)
    async with server:
        await server.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="0.0.0.0:7898")
    ap.add_argument("--target", default="127.0.0.1:7897")
    args = ap.parse_args()

    lh, lp = args.listen.rsplit(":", 1)
    th, tp = args.target.rsplit(":", 1)
    try:
        asyncio.run(serve(lh, int(lp), th, int(tp)))
    except KeyboardInterrupt:
        print("[relay] 收到中断，退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
