"""提取 Qoder 阿里云验证码的初始化参数（给付费解码平台用）。

需要的参数
----------
| 参数 | 来源 |
|---|---|
| sceneId      | 页面里的 requestInfo / initAliyunCaptcha 参数 |
| prefix       | `<prefix>.captcha-open.aliyuncs.com` 的子域名 |
| region       | cn 或 sgp |
| apiGetLib    | AliyunCaptcha.js 的 URL |

做法：拦截所有网络请求 + 扫页面脚本里的初始化代码。

用法：
    python tools/extract_aliyun_params.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIGNUP_URL = "https://qoder.com/users/sign-up"
PROXY = "http://127.0.0.1:7897"
OUT = ROOT / "data" / "aliyun_params"

# 在页面里扫这些键
SCAN_KEYS = ["sceneId", "prefix", "region", "requestInfo", "userUserId",
             "verifyType", "userCertifyId", "traceid", "initAliyunCaptcha",
             "CaptchaSceneId", "sId"]


async def main() -> int:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    reqs: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy={"server": PROXY},
                                           args=["--ignore-certificate-errors"])
        ctx = await browser.new_context(locale="en-US", ignore_https_errors=True,
                                        viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        page.set_default_timeout(30000)

        def on_req(req):
            u = req.url
            if any(k in u for k in ("aliyuncs", "aliyun", "captcha", "FeiLin",
                                    "feilin")):
                reqs.append({"method": req.method, "url": u,
                             "post": (req.post_data or "")[:600]})

        page.on("request", on_req)

        # 走到滑块
        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        await page.locator("#basic_firstName").fill("James")
        await page.locator("#basic_lastName").fill("Smith")
        eb = page.locator("#basic_email")
        await eb.click()
        for ch in "aliyun_param_01@outlook.com":
            await eb.type(ch, delay=28)
        cb = page.locator("input[type=checkbox]").first
        if not await cb.is_checked():
            await cb.check(force=True)
        await asyncio.sleep(0.6)
        await page.get_by_role("button", name="Continue").click()
        await asyncio.sleep(4)
        pw_el = page.locator("input#basic_password, input[autocomplete=new-password]").first
        if await pw_el.count() > 0:
            await pw_el.click()
            for ch in "AliParam123!x":
                await pw_el.type(ch, delay=26)
            await asyncio.sleep(0.6)
            await page.get_by_role("button", name="Continue").click()
            await asyncio.sleep(6)

        # 展开验证码
        for _ in range(4):
            ok = await page.evaluate(
                """() => {
                  const w = document.querySelector('#aliyunCaptcha-window-float');
                  const i = document.querySelector('#aliyunCaptcha-img');
                  return !!(w && w.classList.contains('window-show') &&
                            i && i.getBoundingClientRect().width > 50);
                }""")
            if ok:
                break
            for sel in ["#captcha-element", "#aliyunCaptcha-captcha-left"]:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=4000)
                        break
                except Exception:
                    pass
            await asyncio.sleep(1.8)
        await asyncio.sleep(2)

        # ---- 1. 网络请求里的线索 ----
        print("=== 阿里云相关请求 ===")
        prefixes: set[str] = set()
        lib_urls: set[str] = set()
        for r in reqs:
            u = r["url"]
            print(f"  [{r['method']}] {u[:150]}")
            if r["post"]:
                print(f"        body: {r['post'][:200]}")
            m = re.search(r"https?://([a-z0-9]+)\.captcha-open\.aliyuncs\.com", u)
            if m:
                prefixes.add(m.group(1))
            if "AliyunCaptcha.js" in u or "FeiLin" in u or "aliyunCaptcha" in u:
                lib_urls.add(u.split("?")[0])

        print(f"\n  → prefix 候选: {sorted(prefixes) or '(未从请求中找到)'}")
        print(f"  → lib URL: {sorted(lib_urls)}")

        # ---- 2. 页面脚本里的初始化参数 ----
        print("\n=== 扫描页面脚本里的初始化参数 ===")
        scan = await page.evaluate(
            """(keys) => {
              const out = {};
              // 1) 全局变量
              for (const k of ['requestInfo', 'aliyunCaptchaConfig', 'captchaConfig']) {
                try {
                  if (window[k]) out['global:' + k] = JSON.stringify(window[k]).slice(0, 800);
                } catch (e) {}
              }
              // 2) 页面 HTML 里的字符串
              const html = document.documentElement.innerHTML;
              for (const k of keys) {
                const re = new RegExp(k + '["\\']?\\s*[:=]\\s*["\\']([^"\\']{2,120})["\\']', 'gi');
                const found = [];
                let m;
                let n = 0;
                while ((m = re.exec(html)) !== null && n < 5) {
                  found.push(m[1]);
                  n++;
                }
                if (found.length) out['html:' + k] = found;
              }
              // 3) script 标签内容
              const scripts = [];
              document.querySelectorAll('script:not([src])').forEach(s => {
                const t = s.textContent || '';
                if (/aliyun|sceneId|captcha/i.test(t)) scripts.push(t.slice(0, 1500));
              });
              out['inlineScripts'] = scripts.slice(0, 3);
              // 4) 所有脚本 src
              out['scriptSrcs'] = Array.from(document.querySelectorAll('script[src]'))
                .map(s => s.src).filter(s => /aliyun|captcha|feilin/i.test(s));
              return out;
            }""", SCAN_KEYS)

        for k, v in scan.items():
            print(f"\n  --- {k} ---")
            print(f"    {json.dumps(v, ensure_ascii=False)[:900]}")

        # ---- 3. 从 window 里挖阿里云实例 ----
        print("\n=== 尝试从全局对象挖 sceneId ===")
        dig = await page.evaluate(
            """() => {
              const out = {};
              const seen = new Set();
              const walk = (obj, path, depth) => {
                if (depth > 3 || !obj || typeof obj !== 'object') return;
                if (seen.has(obj)) return;
                seen.add(obj);
                for (const k of Object.keys(obj)) {
                  const v = obj[k];
                  if (/sceneid/i.test(k) && typeof v === 'string') {
                    out[path + '.' + k] = v;
                  }
                  if (/^prefix$/i.test(k) && typeof v === 'string') {
                    out[path + '.' + k] = v;
                  }
                  if (typeof v === 'object') walk(v, path + '.' + k, depth + 1);
                }
              };
              try { walk(window, 'window', 0); } catch (e) {}
              return out;
            }""")
        print(f"  {json.dumps(dig, ensure_ascii=False, indent=2)[:1200]}")

        # 保存
        (OUT / "requests.json").write_text(
            json.dumps(reqs, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / "scan.json").write_text(
            json.dumps(scan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已保存: {OUT}/requests.json, scan.json")

        await browser.close()

    # ---- 汇总 ----
    print("\n=== 给付费平台的参数 ===")
    print(f"  websiteURL = https://qoder.com/users/sign-up")
    print(f"  prefix     = {sorted(prefixes)[0] if prefixes else '[待定]'}")
    print(f"  sceneId    = [从上面 scan 结果里找]")
    print(f"  region     = cn")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
