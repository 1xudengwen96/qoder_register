"""文本导出：账号 / token / 汇总，各自一个 txt，格式固定可被下游直接解析。

为什么用 txt 而不是只有 CSV/JSON
--------------------------------
运营侧要的是「能直接打开、能直接贴、能人眼核对」：

    data/emails.txt            邮箱清单（手工维护）    email----password----clientId----refreshToken
    data/proxies.txt           代理清单（手工/自动）   host:port:user:pass
    data/output/accounts.txt   生成的账号（自动）      email----password----username----state----...
    data/output/tokens.txt     抓到的 token（自动）    email----token----device_id----type----...
    data/output/summary.txt    当前库存统计快照

字段一律用「四个横线」分隔，与邮箱清单保持同一套规则，不用记两套。

每次导出都是**全量重写**（按 id 排序），不是追加——避免同一个账号出现多行、
也避免 token 更新后文件里还是旧值。要审计历史就看 SQLite 的 event_log。
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path

from app.models import Account, AccountState

SEP = "----"
DEFAULT_DIR = Path("data/output")

ACCOUNT_HEADER = [
    "# Qoder 账号清单（自动生成，勿手改；重跑会全量覆盖）",
    "# 格式：email----password----username----state----credits----plan----"
    "expire----register_ip----created_at",
]

TOKEN_HEADER = [
    "# Qoder 设备/账号 token（自动生成，含敏感信息，别外传）",
    "# 格式：email----token----device_id----token_type----updated_at----source",
]


@dataclasses.dataclass(slots=True)
class ExportReport:
    accounts: int = 0
    tokens: int = 0
    files: dict[str, str] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"accounts": self.accounts, "tokens": self.tokens,
                "files": dict(self.files)}


def _dt(value: datetime | None) -> str:
    if value is None:
        return ""
    try:
        return value.isoformat(sep=" ", timespec="seconds")
    except Exception:  # noqa: BLE001
        return str(value)


def _state(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _token_fields(token_json: str | None) -> dict:
    """把 Account.token_json 拆成 token / device_id / type / updated / source。"""
    if not token_json:
        return {}
    try:
        data = json.loads(token_json)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "token": str(data.get("token") or ""),
        "device_id": str(data.get("device_id") or data.get("id") or ""),
        "token_type": str(data.get("token_type") or data.get("type") or ""),
        "updated_at": str(data.get("update_dt") or data.get("updateAt") or ""),
        "source": str(data.get("source") or ""),
    }


def export_accounts(session, path: str | Path = DEFAULT_DIR / "accounts.txt") -> int:
    """全量重写账号清单，返回行数。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = session.query(Account).order_by(Account.id.asc()).all()
    lines = list(ACCOUNT_HEADER)
    for a in rows:
        lines.append(SEP.join([
            a.email or "",
            a.password or "",
            a.username or "",
            _state(a.state),
            str(a.credits or 0),
            a.plan or "",
            _dt(a.trial_expire_at),
            a.register_ip or "",
            _dt(a.created_at),
        ]))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return len(rows)


def export_tokens(session, path: str | Path = DEFAULT_DIR / "tokens.txt") -> int:
    """只导出真拿到 token 的账号，返回行数。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = (session.query(Account)
            .filter(Account.token_json.isnot(None))
            .order_by(Account.id.asc()).all())
    lines = list(TOKEN_HEADER)
    n = 0
    for a in rows:
        f = _token_fields(a.token_json)
        if not f.get("token"):
            continue
        lines.append(SEP.join([
            a.email or "", f["token"], f["device_id"],
            f["token_type"], f["updated_at"], f["source"],
        ]))
        n += 1
    p.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return n


def export_summary(session, path: str | Path = DEFAULT_DIR / "summary.txt") -> int:
    """库存统计快照。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = session.query(Account).all()
    by_state: dict[str, int] = {}
    for a in rows:
        k = _state(a.state)
        by_state[k] = by_state.get(k, 0) + 1
    credited = [a for a in rows if (a.credits or 0) > 0]
    total_credits = sum(a.credits or 0 for a in credited)
    with_token = sum(1 for a in rows if a.token_json)
    lines = [
        "# Qoder 流水线统计快照（自动生成）",
        f"生成时间{SEP}{_dt(datetime.now())}",
        f"账号总数{SEP}{len(rows)}",
        f"已领取{SEP}{len(credited)}",
        f"总积分{SEP}{total_credits}",
        f"有 token{SEP}{with_token}",
    ]
    for k, v in sorted(by_state.items()):
        lines.append(f"状态.{k}{SEP}{v}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return len(rows)


def export_all(session, out_dir: str | Path = DEFAULT_DIR) -> ExportReport:
    """三份文件一起写，返回报告。"""
    d = Path(out_dir)
    rep = ExportReport()
    rep.accounts = export_accounts(session, d / "accounts.txt")
    rep.tokens = export_tokens(session, d / "tokens.txt")
    export_summary(session, d / "summary.txt")
    rep.files = {
        "accounts": str(d / "accounts.txt"),
        "tokens": str(d / "tokens.txt"),
        "summary": str(d / "summary.txt"),
    }
    return rep
