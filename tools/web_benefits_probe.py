"""指纹窗口登录态下 dump 用量/权益页全文与按钮，找试用领取入口。"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app import secrets  # noqa: E402
from app.config import load_config  # noqa: E402
from app.register import bb_cdp  # noqa: E402
from app.register.bitbrowser import BitBrowserClient, resolve_base  # noqa: E402
from app.register.cdp_page import new_page  # noqa: E402

JS = """(() => ({
  url: location.href.slice(0, 120),
  btns: Array.from(document.querySelectorAll('button,a,[role=button]'))
    .map(e => ({t: (e.innerText || '').trim().slice(0, 40),
                href: String(e.getAttribute && e.getAttribute('href') || '').slice(0, 80)}))
    .filter(x => x.t).slice(0, 40),
  body: (document.body ? document.body.innerText : '').slice(0, 1500),
}))()"""

secrets.export_env()
cfg = load_config()
c = BitBrowserClient(resolve_base(cfg))
# argv[1] 可选：账每键（如 acct3）→ 从 .bb_pool_accounts.json 取 env；否则默认 acct3
args = sys.argv[1:]
key = "acct3"
if args and (args[0].startswith("acct") or args[0].startswith("qpool_")):
    key = args.pop(0)
amap = json.loads((ROOT / "data/.bb_pool_accounts.json")
                .read_text(encoding="utf-8"))
ent = amap.get(key) or {"id": "81df28d9934b4726a8bc687f168b5068",
                        "name": "qpool_18"}
print(f"[env] {key} -> {ent}")
launch = bb_cdp.open_for_register(
    c, proxy=None, region="sg",
    env_id=ent["id"], env_name=ent["name"])
page = new_page(launch.endpoint, "about:blank", reuse_blank=True)
urls = args or ["https://qoder.com/account/usage"]
for url in urls:
    page.goto(url, wait_ms=4000)
    out = page.eval(JS) or {}
    print("=" * 70)
    print(json.dumps(out, ensure_ascii=False, indent=1))
page.close()