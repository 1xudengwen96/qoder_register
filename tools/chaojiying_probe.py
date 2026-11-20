"""超级鹰接入探针：先免费验证认证，再拿现场真题离线测识别。

用法（项目根目录）：
    python tools/chaojiying_probe.py            # 只测认证（GetScore，不扣费）
    python tools/chaojiying_probe.py --grid     # 认证通过后用最新九宫格真题测一单（扣题分）
    python tools/chaojiying_probe.py --auth-only

认证背景：官方标准接口是 user+pass/pass2+softid；用户给的 key 格式
（hex32.时间戳.数字）不是官方文档里的认证方式，这里把几种可能的传法
都试一遍，以 err_str 实测为准。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app import secrets                                   # noqa: E402
from app.net.chaojiying import (SCORE, ChaojiyingClient,  # noqa: E402
                                ChaojiyingClick, no_env_session)


def try_auth(label: str, fields: dict) -> dict | None:
    s = no_env_session()
    try:
        r = s.post(SCORE, data=fields, timeout=30,
                   proxies={"http": "", "https": ""})
        d = r.json()
    except Exception as exc:
        print(f"[{label}] 请求异常: {type(exc).__name__}: {exc}")
        return None
    finally:
        s.close()
    ok = d.get("err_no") == 0
    print(f"[{label}] err_no={d.get('err_no')} err_str={d.get('err_str')} "
          f"tifen={d.get('tifen')} {'← 认证通过' if ok else ''}")
    return d if ok else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", action="store_true",
                    help="认证通过后用最新九宫格真题测一单（扣题分）")
    ap.add_argument("--auth-only", action="store_true")
    args = ap.parse_args()

    key = str(secrets.get("captcha.chaojiying.key", "") or "")
    user = str(secrets.get("captcha.chaojiying.user", "") or "")
    pwd = str(secrets.get("captcha.chaojiying.password", "") or "")
    softid = str(secrets.get("captcha.chaojiying.softid", "") or "")
    print(f"key={key[:8]}...{key[-6:]} user={user or '(空)'} "
          f"softid={softid or '(空)'}")

    import hashlib

    good: dict | None = None
    variants: list[tuple[str, dict]] = []
    if user and pwd:
        variants.append(("标准 user+pass2+softid", {
            "user": user, "pass2": hashlib.md5(pwd.encode()).hexdigest(),
            **({"softid": softid} if softid else {})}))
    if key:
        variants += [
            ("key 当 pass（user 空）", {"user": "", "pass": key}),
            ("key 当 user+pass", {"user": key, "pass": key}),
            ("key 当 pass2", {"user": "", "pass2": key}),
            ("key 当 token 附加参数", {"user": "", "pass": "", "token": key}),
            ("key 当 apikey 附加参数", {"user": "", "pass": "", "apikey": key}),
        ]
    for label, fields in variants:
        good = try_auth(label, fields)
        if good:
            break
    if not good:
        print("\n结论：所有传法都未通过认证。超级鹰官方接口要账号+密码"
              "（+软件ID），请提供后填进 config/secrets.yaml 的 "
              "captcha.chaojiying.user/password/softid 再试。")
        return 1
    print(f"\n认证通过，题分余额 {good.get('tifen')}")
    if args.auth_only or not args.grid:
        return 0

    # ---- 九宫格离线真题（最新一组 baxia_grid_*_N.png + _N_q.png）----
    arts = sorted((ROOT / "data/artifacts").glob("baxia_grid_*.png"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    grids = [p for p in arts if not p.stem.endswith("_q")]
    if not grids:
        print("没有九宫格真题产物")
        return 1
    g = grids[0]
    q = g.with_name(g.stem + "_q.png")
    print(f"真题: {g.name} + {q.name if q.exists() else '(无题面)'}")

    cli = ChaojiyingClient(user, pwd, softid, key)
    click = ChaojiyingClick(cli)
    # 落盘合成图供人工核对提示是否醒目
    comp, grid_y0, grid_w = click._composite(
        g.read_bytes(), q.read_bytes() if q.exists() else None)
    out = ROOT / "data/artifacts/cjy_composite_probe.jpg"
    out.write_bytes(comp)
    print(f"合成图已存 {out}（{len(comp)} 字节，格子区起点 y={grid_y0} 格宽图宽={grid_w}）")

    import time

    t0 = time.time()
    sol = click.solve_grid(g.read_bytes(), "",
                           img_instructions=q.read_bytes() if q.exists() else None)
    print(f"结果 cells={sol.cells} points={sol.points} "
          f"耗时={time.time() - t0:.1f}s pic_id={click.last_pic_id}")
    print("raw:", sol.raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())