# -*- coding: utf-8 -*-
r"""邮箱池诊断：在目标机上跑一次，打印取号链路每一环的状态。

用法（在包根目录）：
    python tools/pool_diag.py

依次输出：
1. config 里 mail.provider 当前指向
2. hotmail CSV（控制台导入写入处）行数与首行示例
3. outlook_file 清单文件（mail_list.txt）行数
4. DB 已占用（taken）地址数
5. 实际取号测试：build_mail_layer + acquire(1) 能否拿到一个邮箱
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app import secrets  # noqa: E402
from app.config import get, load_config  # noqa: E402

secrets.export_env()
cfg = load_config()

print("=" * 60)
print("1) mail.provider =", repr(get(cfg, "mail.provider", "")) or "(空=自动)")
print("   严格优先顺序 =", get(cfg, "mail.strict_priority_order", []))

hm_path = ROOT / str(get(cfg, "mail.hotmail.credentials_file",
                         "data/hotmail_accounts.csv"))
print("=" * 60)
print(f"2) hotmail CSV：{hm_path}")
if hm_path.exists():
    lines = [l for l in hm_path.read_text(encoding="utf-8-sig",
                                          errors="replace").splitlines()
             if l.strip()]
    print(f"   共 {len(lines)} 行（含表头）")
    if lines:
        print(f"   首行：{lines[0][:80]}")
        if lines[0].split(",")[0].strip().lower() != "email":
            print("   警告：首行不是 CSV 表头（email,...），生产读取会错位！")
else:
    print("   文件不存在——控制台「导入邮箱池」没写过这里？")

of_path = get(cfg, "mail.outlook_file", "") or ""
print("=" * 60)
print(f"3) outlook_file 清单：{of_path or '(未配置)'}")
if of_path:
    p = Path(of_path)
    if not p.is_absolute():
        p = ROOT / p
    if p.exists():
        n = len([l for l in p.read_text(encoding="utf-8-sig",
                                        errors="replace").splitlines()
                 if l.strip()])
        print(f"   共 {n} 行")
    else:
        print("   文件不存在")

print("=" * 60)
from app.db import create_all, init_engine, session_scope  # noqa: E402
init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
create_all()
from app.models import MailIdentity as MailRow  # noqa: E402
with session_scope() as s:
    total = s.query(MailRow).count()
    from app.models import MailStatus
    pool = s.query(MailRow).filter(MailRow.status == MailStatus.POOL).count()
    print(f"4) DB 邮箱记录：共 {total} 条，其中 POOL（可用）{pool} 条")
    if total:
        for r in s.query(MailRow).limit(5):
            print(f"   - {r.address[:30]}  status={r.status}")

print("=" * 60)
print("5) 实际取号测试：")

from app.db import get_session  # noqa: E402


class _DB2:
    get_session = staticmethod(get_session)


try:
    from app.mail.factory import build_mail_layer
    layer = build_mail_layer(cfg, _DB2())
    print("   可用源：", list(layer.providers.keys())
          if hasattr(layer, "providers") else "(聚合器)")
    idents = layer.acquire(1)
    if idents:
        print(f"   取号成功：{idents[0].address}（provider={idents[0].provider}）")
    else:
        print("   取号失败：池空！——对照上面 2)/3)/4) 看邮箱到底在哪个文件里，")
        print("   而 provider 指向的又是哪条链路。")
except Exception as exc:  # noqa: BLE001
    print(f"   构建失败：{type(exc).__name__}: {exc}")

print("=" * 60)
print("诊断结束。把以上完整输出发回即可定位。")
