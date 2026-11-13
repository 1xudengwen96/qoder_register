"""穷举 ExtractDynamicIP 的业务参数组合，找出正确的 credential 形式。

错误 "Invalid credential type" 说明 server 拿到了请求，但凭据类型参数不对。
可能涉及的字段名/取值：
  extractType:   authentication / whiteList / 1 / 2
  credentialType / credential / type / authType
  generateType:  routine / other

用法：
    python tools/probe_ipdeep_extract.py --key X --secret Y
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.net.ipdeep import IpdeepClient, IpdeepError  # noqa: E402

BASE_BIZ = {
    "format": "info",
    "proxyType": "dynamicResidential",
    "countryCode": "US",
    "protocol": "http",
    "rotationType": "stickiness",
    "duration": 30,
    "num": 1,
}

VARIANTS = [
    {"extractType": "authentication", "generateType": "routine"},
    {"extractType": "whiteList", "generateType": "routine"},
    {"extractType": "1", "generateType": "routine"},
    {"extractType": "2", "generateType": "routine"},
    {"extractType": 1, "generateType": "routine"},
    {"extractType": 2, "generateType": "routine"},
    {"credentialType": "authentication", "generateType": "routine"},
    {"credentialType": "whiteList", "generateType": "routine"},
    {"credentialType": 1, "generateType": "routine"},
    {"credentialType": 2, "generateType": "routine"},
    {"type": "authentication", "generateType": "routine"},
    {"type": "whiteList", "generateType": "routine"},
    {"authType": "authentication", "generateType": "routine"},
    {"authType": "whiteList", "generateType": "routine"},
    {"extractType": "authentication", "generateType": "routine", "credentialType": 1},
    {"extractType": "authentication", "generateType": "routine", "credentialType": "1"},
    {"extractType": "whiteList", "generateType": "routine", "credentialType": 2},
    {"generateType": "routine"},
    {"generateType": "1"},
    {"generateType": 1},
    {},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    args = ap.parse_args()

    c = IpdeepClient(args.key, args.secret, proxy=args.proxy)

    print("=== 穷举 ExtractDynamicIP 业务参数 ===\n")
    hits: list[tuple[dict, str]] = []

    for i, variant in enumerate(VARIANTS, 1):
        biz = {**BASE_BIZ, **variant}
        try:
            data = c.call("DynamicIp", "ExtractDynamicIP", biz)
            print(f"[{i:02d}] OK   {variant}")
            print(f"     → {str(data)[:200]}")
            hits.append((variant, str(data)[:300]))
        except IpdeepError as exc:
            code = exc.code
            mark = "MSG " if code != "40004" else "40004"
            print(f"[{i:02d}] {mark} {variant}")
            print(f"     → code={code} {str(exc)[:90]}")
            if code != "40004":
                hits.append((variant, f"code={code} {exc}"))

    print(f"\n非 40004 的组合：{len(hits)} 个")
    for v, d in hits:
        print(f"  {v} → {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
