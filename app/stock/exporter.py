"""库存与导出：账号分级、到期预警、导出 CSV / JSON。"""

from __future__ import annotations

import csv
import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path

from app.models import Account, AccountState, MailIdentity


@dataclasses.dataclass(slots=True)
class StockRow:
    account_id: int
    email: str
    password: str
    credits: int
    plan: str
    state: str
    trial_expire_at: str
    days_left: int
    provider: str
    grade: str


def grade_of(days_left: int, credits: int) -> str:
    """库存分级：A 优质（天数足额度满）、B 常规、C 临期、D 无值。"""
    if credits <= 0:
        return "D"
    if days_left >= 10:
        return "A"
    if days_left >= 4:
        return "B"
    if days_left >= 1:
        return "C"
    return "D"


class StockManager:
    def __init__(self, session) -> None:
        self.s = session

    def _rows(self) -> list[StockRow]:
        now = datetime.now()
        out: list[StockRow] = []
        q = (
            self.s.query(Account)
            .filter(Account.state.in_([AccountState.CLAIMED, AccountState.STOCK]))
            .all()
        )
        for a in q:
            prov = (
                self.s.query(MailIdentity.provider)
                .filter(MailIdentity.qoder_account_id == a.id)
                .scalar()
            )
            expire = a.trial_expire_at
            days_left = (expire - now).days if expire else 0
            out.append(
                StockRow(
                    account_id=a.id,
                    email=a.email,
                    password=a.password or "",
                    credits=a.credits,
                    plan=a.plan or "",
                    state=a.state.value,
                    trial_expire_at=expire.isoformat(sep=" ", timespec="seconds") if expire else "",
                    days_left=days_left,
                    provider=prov.value if hasattr(prov, "value") else (prov or ""),
                    grade=grade_of(days_left, a.credits),
                )
            )
        out.sort(key=lambda r: (r.grade, -r.days_left))
        return out

    # ---- 导出 -------------------------------------------------------------
    def export_csv(self, out_path: str | Path) -> int:
        rows = self._rows()
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["account_id", "email", "password", "credits", "plan", "state",
                        "trial_expire_at", "days_left", "provider", "grade"])
            for r in rows:
                w.writerow([r.account_id, r.email, r.password, r.credits, r.plan,
                            r.state, r.trial_expire_at, r.days_left, r.provider, r.grade])
        return len(rows)

    def export_json(self, out_path: str | Path, include_token: bool = False) -> int:
        rows = self._rows()
        data = []
        for r in rows:
            item = dataclasses.asdict(r)
            if include_token:
                a = self.s.get(Account, r.account_id)
                item["token_json"] = a.token_json if a else None
            data.append(item)
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(data)

    # ---- 预警 -------------------------------------------------------------
    def expiring_soon(self, within_days: int = 3) -> list[StockRow]:
        cutoff = datetime.now() + timedelta(days=within_days)
        return [
            r for r in self._rows()
            if r.trial_expire_at and datetime.fromisoformat(r.trial_expire_at) <= cutoff
        ]

    def summary(self) -> str:
        rows = self._rows()
        if not rows:
            return "库存为空"
        by_grade: dict[str, int] = {}
        for r in rows:
            by_grade[r.grade] = by_grade.get(r.grade, 0) + 1
        total_credits = sum(r.credits for r in rows)
        grades = " ".join(f"{k}:{v}" for k, v in sorted(by_grade.items()))
        return f"库存 {len(rows)} 个号，总积分 {total_credits}，分级 {grades}"
