"""比特浏览器 API 深度探测：把每个接口的完整参数与返回结构搞清楚。

重点解决：
1. /browser/open 的完整参数（含 CDP 相关开关）
2. "窗口被限制打开" 的真实原因
3. 返回结构里 CDP 端点叫什么字段
4. 建环境时最少要传哪些字段

用法：
    python tools/probe_bitbrowser_deep.py
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import requests


class BB:
    def __init__(self, base: str = "http://127.0.0.1:54345") -> None:
        self.base = base.rstrip("/")

    def post(self, path: str, body: Any = None, timeout: int = 60) -> dict:
        try:
            r = requests.post(self.base + path, json=body or {}, timeout=timeout)
            return r.json()
        except Exception as exc:
            return {"success": False, "msg": f"{type(exc).__name__}: {exc}", "_err": True}


DEFAULT_FP = {
    "coreVersion": "131",
    "ostype": "PC",
    "os": "Win32",
    "isIpv6": False,
    "isRandomFinger": False,
    "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "screenWidth": 1920,
    "screenHeight": 1080,
    "fonts": ["Arial", "Verdana", "Segoe UI"],
    "language": ["en-US", "en"],
    "timeZone": "America/New_York",
    "webRTC": "disabled",
    "canvas": "1",
    "webGL": "1",
    "audioContext": "1",
    "hardwareConcurrency": 8,
    "deviceMemory": 8,
    "doNotTrack": False,
    "position": "1",
    "browserKernel": "chrome",
}


def show(title: str, obj: Any, keys_only: bool = False) -> None:
    print(f"\n--- {title} ---")
    if keys_only and isinstance(obj, dict):
        print("keys:", sorted(obj.keys()))
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2)[:3000])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:54345")
    args = ap.parse_args()
    bb = BB(args.base)

    # ---------- 1. 健康 ----------
    print("=== 1. /health ===")
    h = bb.post("/health")
    print(h)

    # ---------- 2. 列表结构 ----------
    print("\n=== 2. /browser/list 返回结构 ===")
    lst = bb.post("/browser/list", {"page": 0, "pageSize": 5})
    data = lst.get("data") or {}
    print("data keys:", sorted(data.keys()))
    items = data.get("list") or []
    print(f"totalNum={data.get('totalNum')}, 本页 {len(items)} 条")
    if items:
        show("单条环境的完整字段", items[0], keys_only=True)
        fp = items[0].get("browserFingerPrint") or {}
        show("browserFingerPrint 完整字段", fp, keys_only=True)

    # ---------- 3. 建一个干净环境 ----------
    print("\n=== 3. 创建环境 ===")
    name = f"qoder_probe_{len(items)}"
    created = bb.post("/browser/update", {
        "name": name,
        "remark": "deep-probe",
        "proxyMethod": 3,
        "proxyType": "noproxy",
        "browserFingerPrint": DEFAULT_FP,
    })
    print("success:", created.get("success"), "msg:", created.get("msg"))
    env = created.get("data") or {}
    env_id = env.get("id")
    print("env id:", env_id)

    if not env_id:
        print("建环境失败，后续跳过")
        return 1

    # ---------- 4. 启动：逐个试参数 ----------
    print("\n=== 4. /browser/open 参数探测 ===")
    open_variants = [
        ("最小", {"id": env_id}),
        ("queue=false", {"id": env_id, "queue": False}),
        ("queue=true", {"id": env_id, "queue": True}),
        ("带 args 空数组", {"id": env_id, "args": []}),
        ("带 ignoreDefaultUrls", {"id": env_id, "ignoreDefaultUrls": False}),
        ("带 headless", {"id": env_id, "headless": False}),
        ("带 全部常见参数", {
            "id": env_id, "queue": True, "ignoreDefaultUrls": False,
            "args": [], "headless": False,
        }),
    ]
    for label, body in open_variants:
        r = bb.post("/browser/open", body)
        d = r.get("data") or {}
        print(f"  [{label:<16}] success={r.get('success')} msg={r.get('msg','')} "
              f"http={d.get('http','')} ws={d.get('ws','')} pid={d.get('pid','')}")
        if r.get("success"):
            show("启动成功返回结构", d, keys_only=True)

    # ---------- 5. 详情 ----------
    print("\n=== 5. /browser/detail ===")
    det = bb.post("/browser/detail", {"id": env_id})
    print("success:", det.get("success"), "msg:", det.get("msg"))
    if det.get("data"):
        show("detail 返回字段", det["data"], keys_only=True)

    # ---------- 6. 运行中窗口 ----------
    print("\n=== 6. /browser/pids ===")
    pids = bb.post("/browser/pids", {"ids": [env_id]})
    print(json.dumps(pids, ensure_ascii=False)[:800])

    # ---------- 7. 分组 ----------
    print("\n=== 7. /group/list ===")
    grp = bb.post("/group/list", {"page": 0, "pageSize": 50})
    print("success:", grp.get("success"), "msg:", grp.get("msg"))
    if grp.get("data"):
        print(json.dumps(grp["data"], ensure_ascii=False)[:800])

    # ---------- 8. 指纹随机 ----------
    print("\n=== 8. /browser/fingerprint/random ===")
    rnd = bb.post("/browser/fingerprint/random", {"browserId": env_id})
    print("success:", rnd.get("success"), "msg:", rnd.get("msg"))
    if rnd.get("data"):
        d = rnd["data"]
        if isinstance(d, dict):
            for k in ("ua", "timeZone", "language", "screenWidth", "webGLManufacturer", "webGLRender"):
                if k in d:
                    print(f"  {k}: {d[k]}")
            print("  全部字段:", sorted(d.keys()))

    # ---------- 9. 清理 ----------
    print("\n=== 9. 清理测试环境 ===")
    cl = bb.post("/browser/close", {"id": env_id})
    print("close:", cl.get("success"), cl.get("msg"))
    de = bb.post("/browser/delete", {"id": env_id})
    print("delete:", de.get("success"), de.get("msg"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
