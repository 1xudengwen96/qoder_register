"""枚举 Qoder 进程窗口 + 尝试激活隐藏窗口后 dump 空 URL target。"""
import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

u = ctypes.windll.user32
pids = set()
out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Qoder.exe",
                      "/FO", "CSV", "/NH"], capture_output=True, text=True)
for line in out.stdout.splitlines():
    parts = [p.strip('"') for p in line.split('","')]
    if len(parts) > 1 and parts[1].isdigit():
        pids.add(int(parts[1]))
print("pids:", sorted(pids))

res = []


@ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
def cb(h, l):
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    if pid.value in pids:
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        res.append((h, bool(u.IsWindowVisible(h)), buf.value[:60]))
    return True


u.EnumWindows(cb, 0)
for r in res:
    print("win:", r)

# 激活所有不可见窗口（权益中心窗口可能 lazy-load 直到 show）
for h, vis, title in res:
    if not vis:
        u.ShowWindow(h, 9)  # SW_RESTORE
        u.SetForegroundWindow(h)
        print("activated:", h, title)
time.sleep(3)

from app.register.cdp_page import CDPPage  # noqa: E402

ts = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5))
for t in ts:
    if t.get("type") != "page":
        continue
    print("target:", (t.get("url") or "(empty)")[:90])
    if t.get("url"):
        continue
    try:
        p = CDPPage("http://127.0.0.1:9222", ws_url=t["webSocketDebuggerUrl"],
                    timeout=15)
        print(json.dumps(p.eval("""(() => ({
          url: location.href.slice(0, 120),
          btns: Array.from(document.querySelectorAll('button,a,[role=button]'))
            .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 30),
          body: (document.body ? document.body.innerText : '').slice(0, 900),
        }))()""") or {}, ensure_ascii=False, indent=1))
        p.close()
    except Exception as exc:  # noqa: BLE001
        print("ERR", type(exc).__name__, exc)