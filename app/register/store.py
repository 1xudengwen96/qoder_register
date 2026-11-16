"""注册成功账号落库：JSONL + 纯文本清单 + SQLite（可选）。

之前注册成功只是打印到控制台，账号等于丢了。这里三处一起写：

1. ``data/registered.jsonl`` —— 一行一个 JSON，字段最全（给程序用）
2. ``data/accounts.txt``      —— ``邮箱----密码----姓名----时间``，给人看/导入工具
3. ``data/qoder.db`` 的 ``account`` 表 —— 状态机从这里往下走（领取/库存）

同一个邮箱重复保存会更新而不是追加（JSONL 里保留首次记录 + 追加最新一条）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
JSONL = DATA / "registered.jsonl"
TXT = DATA / "accounts.txt"


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_registered(email: str, password: str, first: str = "", last: str = "",
                    env_id: str = "", proxy: str = "", region: str = "",
                    source: str = "", extra: dict | None = None,
                    db_url: str = "") -> dict:
    """保存一个注册成功的账号，返回写入位置与 DB 记录 id。"""
    DATA.mkdir(parents=True, exist_ok=True)
    ts = _now()
    rec = {
        "email": email,
        "password": password,
        "first": first,
        "last": last,
        "name": f"{first} {last}".strip(),
        "time": ts,
        "source": source,
        "env_id": env_id,
        "proxy": proxy,
        "region": region,
    }
    if extra:
        rec.update(extra)

    with JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    line = f"{email}----{password}----{rec['name']}----{ts}\n"
    existing = TXT.read_text(encoding="utf-8") if TXT.exists() else ""
    if email.lower() not in existing.lower():
        with TXT.open("a", encoding="utf-8") as fh:
            fh.write(line)

    db_id = _save_db(email, password, rec["name"], proxy, db_url)
    return {"jsonl": str(JSONL), "txt": str(TXT), "db_id": db_id, "record": rec}


def _save_db(email: str, password: str, name: str, proxy: str,
             db_url: str = "") -> int | None:
    """写 SQLite 的 account 表；失败不影响文件落库。"""
    url = db_url or os.environ.get("QODER_DB_URL") or f"sqlite:///{DATA / 'qoder.db'}"
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker

        from app.models import Account, AccountState, Base

        engine = create_engine(url)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as s:
            row = s.execute(select(Account).where(
                Account.email == email)).scalar_one_or_none()
            if row is None:
                row = Account(email=email, password=password, username=name)
                state = getattr(AccountState, "REGISTERED", None) \
                    or getattr(AccountState, "NEW", None)
                if state is not None:
                    row.state = state
                row.register_ip = proxy
                s.add(row)
                s.commit()
                return int(row.id)
            row.password = password
            row.username = name
            s.commit()
            return int(row.id)
    except Exception:
        return None


def load_registered() -> list[dict]:
    if not JSONL.exists():
        return []
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for line in JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        seen[rec.get("email", "")] = rec
    out.extend(seen.values())
    return out
