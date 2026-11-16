"""账号/token/统计 的 txt 导出格式。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Account, AccountState, Base
from app.stock.txt_export import SEP, export_accounts, export_all, export_tokens


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _data_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8-sig").splitlines()
            if ln and not ln.startswith("#")]


class TestExport(unittest.TestCase):
    def setUp(self):
        self.s = _session()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

        ok = Account(
            email="a1@outlook.com", password="P@ss1", username="u1",
            state=AccountState.STOCK, credits=2000, plan="pro_trial",
            trial_expire_at=datetime(2026, 12, 1, 10, 0, 0),
            register_ip="1.1.1.1:8080", created_at=datetime(2026, 11, 13, 22, 0, 0),
            token_json=json.dumps({
                "token": "P1gTOKEN", "device_id": "dev-1", "token_type": "t1",
                "update_dt": "2026-11-13 22:10:00", "source": "machine_token",
            }),
        )
        dead = Account(email="a2@outlook.com", password="P@ss2", username="u2",
                       state=AccountState.DEAD, credits=0)
        self.s.add_all([ok, dead])
        self.s.commit()

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_accounts_line_format(self):
        n = export_accounts(self.s, self.dir / "accounts.txt")
        self.assertEqual(n, 2)
        lines = _data_lines(self.dir / "accounts.txt")
        self.assertEqual(len(lines), 2)
        parts = lines[0].split(SEP)
        self.assertEqual(len(parts), 9, "email/pass/user/state/credits/plan/expire/ip/created")
        self.assertEqual(parts[0], "a1@outlook.com")
        self.assertEqual(parts[3], "stock")
        self.assertEqual(parts[4], "2000")
        self.assertEqual(parts[7], "1.1.1.1:8080")
        self.assertTrue(parts[8].startswith("2026-11-13 22:00:00"))

    def test_tokens_only_for_accounts_with_token(self):
        n = export_tokens(self.s, self.dir / "tokens.txt")
        self.assertEqual(n, 1)
        lines = _data_lines(self.dir / "tokens.txt")
        self.assertEqual(len(lines), 1)
        parts = lines[0].split(SEP)
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0], "a1@outlook.com")
        self.assertEqual(parts[1], "P1gTOKEN")
        self.assertEqual(parts[2], "dev-1")
        self.assertEqual(parts[5], "machine_token")

    def test_export_all_writes_three_files(self):
        rep = export_all(self.s, self.dir)
        self.assertEqual(rep.accounts, 2)
        self.assertEqual(rep.tokens, 1)
        for key in ("accounts", "tokens", "summary"):
            self.assertTrue(Path(rep.files[key]).exists(), key)
        summary = (self.dir / "summary.txt").read_text(encoding="utf-8-sig")
        self.assertIn("账号总数", summary)
        self.assertIn("状态.dead", summary)

    def test_rewrite_is_idempotent(self):
        export_all(self.s, self.dir)
        first = (self.dir / "accounts.txt").read_text(encoding="utf-8-sig")
        export_all(self.s, self.dir)
        self.assertEqual(first, (self.dir / "accounts.txt").read_text(encoding="utf-8-sig"))

    def test_no_token_file_only_header(self):
        for a in self.s.query(Account).all():
            a.token_json = None
        self.s.commit()
        n = export_tokens(self.s, self.dir / "tokens.txt")
        self.assertEqual(n, 0)
        self.assertEqual(_data_lines(self.dir / "tokens.txt"), [])


if __name__ == "__main__":
    unittest.main()
