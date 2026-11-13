"""比特浏览器本地 API 全量普查。

做法：拿一组候选路径逐个打，记录哪些存在、返回什么、错误提示里缺哪个参数。
比特浏览器本地服务是 Spring Boot 风格，错误信息会明确告诉你缺哪个字段，
所以"故意打错"就是最好的接口发现手段。

用法：
    python tools/probe_bitbrowser.py
    python tools/probe_bitbrowser.py --base http://127.0.0.1:54345
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import requests

CANDIDATES = [
    # ---- 健康 / 版本 ----
    ("POST", "/health", {}),
    ("GET", "/health", None),
    ("POST", "/browser/version", {}),
    ("GET", "/api/version", None),

    # ---- 环境列表 / 查询 ----
    ("POST", "/browser/list", {"page": 0, "pageSize": 10}),
    ("POST", "/browser/list", {"page": 0, "pageSize": 10, "groupId": ""}),
    ("POST", "/browser/list", {"page": 0, "pageSize": 10, "name": ""}),
    ("POST", "/browser/list", {"page": 0, "pageSize": 10, "seq": []}),
    ("POST", "/browser/detail", {"id": ""}),
    ("POST", "/browser/detail", {}),

    # ---- 创建 / 修改 / 删除 ----
    ("POST", "/browser/update", {}),
    ("POST", "/browser/delete", {}),
    ("POST", "/browser/delete", {"id": ""}),

    # ---- 启动 / 关闭 ----
    ("POST", "/browser/open", {}),
    ("POST", "/browser/close", {}),
    ("POST", "/browser/close", {"id": ""}),
    ("POST", "/browser/close/all", {}),

    # ---- 分组 ----
    ("POST", "/browser/group/list", {}),
    ("GET", "/browser/group/list", None),
    ("POST", "/group/list", {}),

    # ---- 代理 ----
    ("POST", "/proxy/list", {"page": 0, "pageSize": 10}),
    ("POST", "/browser/proxy/check", {}),
    ("POST", "/browser/proxy/check", {"host": "", "port": ""}),

    # ---- 指纹 ----
    ("POST", "/browser/fingerprint/random", {}),
    ("POST", "/browser/fingerprint/random", {"browserId": ""}),

    # ---- 其它常用 ----
    ("POST", "/browser/update/remark", {}),
    ("POST", "/browser/cookie/get", {}),
    ("POST", "/browser/tabs", {}),
    ("POST", "/browser/pids", {}),
    ("POST", "/browser/running", {}),
]


def probe(base: str, method: str, path: str, body: Any, timeout: int = 15) -> dict:
    url = base.rstrip("/") + path
    try:
        if method == "GET":
            r = requests.get(url, timeout=timeout)
        else:
            r = requests.post(url, json=body if body is not None else {},
                              timeout=timeout)
    except Exception as exc:
        return {"_err": f"{type(exc).__name__}: {exc}"}

    out: dict = {"_status": r.status_code}
    try:
        data = r.json()
        out["_json"] = data
        if isinstance(data, dict):
            out["_success"] = data.get("success")
            if "msg" in data:
                out["_msg"] = data["msg"]
            if "data" in data and data["data"] is None:
                out["_data_none"] = True
    except Exception:
        out["_text"] = r.text[:200]
    return out


def classify(res: dict) -> str:
    if "_err" in res:
        return f"连接失败 {res['_err'][:60]}"
    st = res.get("_status")
    if st == 404:
        return "不存在(404)"
    if st == 405:
        return "方法不对(405)"
    if st and st >= 500:
        return f"服务端错误({st}) {res.get('_msg','')}"
    if res.get("_success") is True:
        return "OK"
    if res.get("_success") is False:
        return f"存在, 参数不足/业务拒绝: {res.get('_msg','')}"
    return f"HTTP {st} 非标准响应"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:54345")
    args = ap.parse_args()

    print(f"=== 比特浏览器 API 普查 @ {args.base} ===\n")
    seen: dict[tuple[str, str], str] = {}

    for method, path, body in CANDIDATES:
        key = (method, path)
        if key in seen:
            continue
        res = probe(args.base, method, path, body)
        verdict = classify(res)
        seen[key] = verdict
        marker = "✓" if verdict == "OK" else ("·" if "参数不足" in verdict else "✗")
        print(f"[{marker}] {method:<4} {path:<36} {verdict}")

    # 再对"参数不足"的接口逐个补参数，看缺什么
    print("\n=== 参数缺口详解 ===")
    for method, path, body in CANDIDATES:
        if method != "POST":
            continue
        res = probe(args.base, method, path, body)
        msg = res.get("_msg", "")
        if msg and ("请传入" in msg or "不能为空" in msg or "缺少" in msg):
            print(f"- {path:<36} 缺: {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
