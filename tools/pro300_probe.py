# -*- coding: utf-8 -*-
"""取证：300 分 Pro 会员活动的客户端可见状态。

- 遍历 9222 所有 page target，抓正文里含 300/Pro/领取/活动/权益/Credits 的片段
- 落盘 %APPDATA%\\com.qoder.app.stable 的 onboarding/权益类文件内容
只做取证，不改动任何状态。
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(1, ".pylibs")
from app.register.cdp_page import CDPPage  # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "9222"
KEYS = ("300", "Pro", "pro", "领取", "活动", "权益", "试用", "Credits",
        "credits", "benefits", "trial", "Trial")
OUT = Path("data/logs/pro300_probe.txt")


def targets():
    return json.load(urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}/json/list", timeout=5))


def main():
    lines = []

    def w(s):
        lines.append(s)

    for t in targets():
        if t.get("type") != "page":
            continue
        w("=" * 70)
        w(f"[target] {t.get('title','')[:60]}")
        w(f"[url] {t.get('url','')[:160]}")
        try:
            c = CDPPage(f"http://127.0.0.1:{PORT}",
                        ws_url=t["webSocketDebuggerUrl"], timeout=10)
            body = c.eval(
                "document.body ? document.body.innerText : ''") or ""
            hits = []
            for para in body.splitlines():
                p = para.strip()
                if not p:
                    continue
                if any(k in p for k in KEYS):
                    hits.append(p[:200])
            w("[hits] " + (json.dumps(hits[:40], ensure_ascii=False)
                           if hits else "(无关键词)"))
            c.close()
        except Exception as exc:  # noqa: BLE001
            w(f"[eval 失败] {exc}")

    app = Path(os.environ["APPDATA"]) / "com.qoder.app.stable"
    w("=" * 70)
    w(f"[disk] {app}")
    for pat in ("first-launch-onboarding*.json", "*onboarding*.v1.json",
                "auth-profile-overlays*.dat", "qoder-data.v1.json",
                "auth.machine-id", "sessionMigration*"):
        for p in sorted(app.glob(pat)):
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                txt = "(read fail)"
            w(f"--- {p.name} ({p.stat().st_size}B) ---")
            w(txt[:1500])
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"written {OUT} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
