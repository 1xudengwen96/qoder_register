"""批量体检代理：出口 IP / 国家 / 时区 / 延迟，以及目标站可达性。

    python tools/proxy_check.py                     # 读 config 里的 proxy.file
    python tools/proxy_check.py --file data/proxies.txt
    python tools/proxy_check.py --target https://qoder.com/
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get, load_config  # noqa: E402
from app.net.http import no_env_session  # noqa: E402
from app.net.proxy import ProxyPool, parse_line  # noqa: E402

GEO = "https://api.ip.sb/geoip"


def check_one(px, target: str, timeout: int, retries: int = 1) -> dict:
    url = px.requests_proxies()
    out = {"label": px.label, "raw": px.raw, "proxy": url.get("https"),
           "ip": "", "country": "", "tz": "", "geo_ms": 0,
           "target_ok": not bool(target), "target_status": 0, "target_ms": 0,
           "error": "", "tries": 0}
    with no_env_session() as s:
        for attempt in range(1, retries + 1):
            out["tries"] = attempt
            t0 = time.time()
            try:
                r = s.get(GEO, proxies=url, timeout=timeout)
                j = r.json()
                out.update(ip=str(j.get("ip", "")),
                           country=str(j.get("country_code", "")),
                           tz=str(j.get("timezone", "")),
                           geo_ms=int((time.time() - t0) * 1000),
                           error="")
                break
            except Exception as exc:
                out["error"] = f"{type(exc).__name__}: {str(exc)[:60]}"
                if attempt < retries:
                    time.sleep(1.5)
        if not out["ip"]:
            return out
        if target:
            t1 = time.time()
            try:
                r2 = s.get(target, proxies=url, timeout=timeout)
                out.update(target_ok=r2.status_code < 400,
                           target_status=r2.status_code,
                           target_ms=int((time.time() - t1) * 1000))
            except Exception as exc:
                out["error"] = f"目标站 {type(exc).__name__}: {str(exc)[:50]}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="")
    ap.add_argument("--target", default="https://qoder.com/")
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--json", default="", help="把结果写到文件")
    ap.add_argument("--limit", type=int, default=0, help="只测前 N 条（调试用）")
    ap.add_argument("--retries", type=int, default=2, help="每条重试次数")
    args = ap.parse_args()

    path = args.file or get(load_config(), "proxy.file", "data/proxies.txt")
    pool = ProxyPool.from_file(path)
    print(f"代理文件: {path}")
    print(f"- {pool.summary()}")
    if not pool.proxies:
        raw_lines = [l.strip() for l in Path(path).read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.strip().startswith("#")]
        print(f"文件里有 {len(raw_lines)} 行非注释，但解析出 0 条。样例：")
        for line in raw_lines[:3]:
            print(f"  {line[:80]}  ->  {parse_line(line)}")
        return 2

    target = "" if args.target in ("", "-") else args.target
    targets = [(px, target) for px in pool.proxies]
    if args.limit:
        targets = targets[:args.limit]
    rows: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(check_one, px, tgt, args.timeout, args.retries): px
                for px, tgt in targets}
        for fut in cf.as_completed(futs):
            rows.append(fut.result())

    rows.sort(key=lambda r: r["label"])
    ok = [r for r in rows if r["ip"] and r["target_ok"]]
    geo_ok = [r for r in rows if r["ip"]]
    print(f"\n{'序号':<4} {'出口 IP':<16} {'国家':<4} {'时区':<18} "
          f"{'geo':>5} {'目标':>5} {'状态':>5} {'耗时':>6}  备注")
    for i, r in enumerate(rows, 1):
        label = r["label"].split("/")[-1][:28]
        print(f"{i:<4} {r['ip'] or '-':<16} {r['country'] or '-':<4} "
              f"{r['tz'] or '-':<18} {r['geo_ms'] or 0:>5} "
              f"{'OK' if r['target_ok'] else 'NO':>5} "
              f"{r['target_status'] or 0:>5} {r['target_ms'] or 0:>6}  "
              f"{label} {r['error'][:40]}")

    print(f"\n- 可用（出口+目标站都通）: {len(ok)}/{len(rows)}")
    print(f"- 仅出口可用: {len(geo_ok)}/{len(rows)}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"- 明细已写入 {args.json}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
