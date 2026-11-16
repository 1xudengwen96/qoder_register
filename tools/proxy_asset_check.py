"""按代理测关键资源可达性：阿里云验证码 CDN / qoder 站点。

    python tools/proxy_asset_check.py
"""

from __future__ import annotations

import concurrent.futures as cf
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get, load_config  # noqa: E402
from app.net.http import no_env_session  # noqa: E402
from app.net.proxy import ProxyPool  # noqa: E402

TARGETS = (
    ("aliyun-js", "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"),
    ("aliyun-img", "https://g.alicdn.com/captcha-frontend/FeiLin/1.5.4/feilin076.dfd7a591c9db19810526fb604370c75de8180c860b769065ed2bf50d727dc4b1.js"),
    ("qoder", "https://qoder.com/"),
)


def check(px) -> dict:
    proxies = px.requests_proxies()
    row = {"label": px.label, "raw": px.raw, "ok": 0, "detail": []}
    with no_env_session() as s:
        for name, url in TARGETS:
            t0 = time.time()
            try:
                r = s.get(url, proxies=proxies, timeout=20)
                ok = r.status_code < 400 and len(r.content) > 200
                row["ok"] += 1 if ok else 0
                row["detail"].append(f"{name}:{'OK' if ok else r.status_code}"
                                     f"/{int((time.time()-t0)*1000)}ms")
            except Exception as exc:
                row["detail"].append(f"{name}:{type(exc).__name__}")
    return row


def main() -> int:
    cfg = load_config()
    pool = ProxyPool.from_file(get(cfg, "proxy.file", "data/proxies.txt"))
    print(f"代理 {len(pool.proxies)} 条")
    rows: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for r in ex.map(check, pool.proxies):
            rows.append(r)
    rows.sort(key=lambda r: -r["ok"])
    for i, r in enumerate(rows, 1):
        print(f"{i:<3} ok={r['ok']}/3  {' '.join(r['detail'])}  {r['label']}")
    best = [r for r in rows if r["ok"] == 3]
    print(f"\n三项全通: {len(best)}/{len(rows)}")
    if best:
        print("推荐:", best[0]["raw"][:110])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
