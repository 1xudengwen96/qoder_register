"""把 data/proxies.txt 的四段冒号格式转成 socks5:// URL 形式（幂等）。

8082 是 IPDEEP 的 SOCKS5 端口；写成 ``host:port:user:pass`` 会被当成 HTTP
代理，导致全部超时。转成 URL 形式后 scheme 明确。

    python scripts/convert_proxies.py                 # 默认 data/proxies.txt
    python scripts/convert_proxies.py --port 8082     # 只转这个端口
"""

from __future__ import annotations

import argparse
import pathlib

HEADER = [
    "# IPDEEP 代理清单（一条一用）",
    "# 格式：socks5://user:pass@host:port   或   http://user:pass@host:port",
    "# 说明：IPDEEP 的 8082 端口是 SOCKS5，必须写 socks5://；8080 走 HTTP",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="data/proxies.txt")
    ap.add_argument("--port", default="", help="只转换该端口（留空=全部四段行）")
    args = ap.parse_args()

    path = pathlib.Path(args.file)
    lines = path.read_text(encoding="utf-8").splitlines()
    body: list[str] = []
    converted = 0
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "://" in s:
            body.append(s)
            continue
        parts = s.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            if args.port and port != args.port:
                body.append(s)
                continue
            scheme = "socks5" if port in ("8082", "1080") else "http"
            body.append(f"{scheme}://{user}:{pwd}@{host}:{port}")
            converted += 1
        else:
            body.append(s)

    path.write_text("\n".join(HEADER + body) + "\n", encoding="utf-8")
    print(f"{path}: 转换 {converted} 条，共 {len(body)} 条")
    for line in body[:3]:
        print("  " + line[:100])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
