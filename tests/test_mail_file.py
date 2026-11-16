"""邮箱清单（email----pass----clientId----refreshToken）解析与取用。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.mail.outlook_file import OutlookFileProvider, audit

LINE4 = ("a1@outlook.com----Passw0rd!----client-aaa----refresh-aaa")
LINE6 = ("a2@outlook.com----Pass2----client-bbb----refresh-bbb----rec@outlook.com----RecPass")
NO_OAUTH = "a3@outlook.com----Pass3----"
DUPE = "a1@outlook.com----other----client-ccc----refresh-ccc"
GARBAGE = "这一行不是邮箱"


def _write(body: str) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d) / "emails.txt"
    p.write_text(body, encoding="utf-8")
    return p


class TestAudit(unittest.TestCase):
    def test_counts_and_duplicates(self):
        p = _write("\n".join([
            "# 注释行", LINE4, LINE6, NO_OAUTH, DUPE, GARBAGE, "",
        ]))
        rep = audit(p)
        self.assertEqual(rep.total, 4)          # a1/a2/a3/a1(dup)
        self.assertEqual(rep.usable, 3)         # a1 / a2 / a1(重复行) 都带 OAuth
        self.assertEqual(rep.no_oauth, 1)
        self.assertEqual(rep.duplicates, ["a1@outlook.com"])
        self.assertEqual(len(rep.bad_lines), 1)

    def test_missing_file(self):
        rep = audit("data/definitely-not-here.txt")
        self.assertEqual(rep.total, 0)
        self.assertEqual(rep.usable, 0)

    def test_shipped_file_parses_clean(self):
        p = Path("data/emails.txt")
        if not p.exists():
            self.skipTest("没有 data/emails.txt")
        rep = audit(p)                      # 用户填的数据不许有格式错行
        self.assertEqual(rep.bad_lines, [])
        self.assertEqual(rep.duplicates, [])
        self.assertEqual(rep.no_oauth, 0)


class TestProvider(unittest.TestCase):
    def test_acquire_skips_taken_and_marks_used(self):
        p = _write("\n".join([LINE4, LINE6]))
        prov = OutlookFileProvider(p, taken_addresses={"a1@outlook.com"})
        got = prov.acquire(5)
        self.assertEqual([i.address for i in got], ["a2@outlook.com"])
        self.assertEqual(got[0].provider, "outlook_file")
        self.assertTrue(got[0].meta["has_oauth"])
        self.assertEqual(got[0].meta["client_id"], "client-bbb")
        # 第二次不能拿到同一个
        self.assertEqual(prov.acquire(5), [])

    def test_health_reports_oauth_gap(self):
        p = _write("\n".join([LINE4, NO_OAUTH]))
        prov = OutlookFileProvider(p)
        h = prov.health()
        self.assertEqual(h.total, 2)
        self.assertEqual(h.remaining, 1)
        self.assertTrue(h.ok)
        self.assertIn("缺 refreshToken", h.detail)

    def test_reload_picks_up_new_lines(self):
        p = _write(LINE4)
        prov = OutlookFileProvider(p)
        self.assertEqual(prov.health().total, 1)
        p.write_text(LINE4 + "\n" + LINE6, encoding="utf-8")
        import os
        os.utime(p, (0, 0))                 # 保证 mtime 变化
        self.assertEqual(prov.refresh(), 2)
        self.assertEqual(prov.health().total, 2)

    def test_wait_for_mail_without_oauth_returns_none(self):
        p = _write(NO_OAUTH)
        prov = OutlookFileProvider(p)
        ident = prov.acquire(1)[0]
        self.assertFalse(ident.meta["has_oauth"])
        self.assertIsNone(prov.wait_for_mail(ident, timeout=1))


class TestLoadAccounts(unittest.TestCase):
    def test_comment_lines_are_not_accounts(self):
        from app.mail.outlook_oauth import load_accounts

        p = _write("# 示例 abc@outlook.com----pwd----cid----rt\n" + LINE4)
        accs = load_accounts(p)
        self.assertEqual([a.email for a in accs], ["a1@outlook.com"])


if __name__ == "__main__":
    unittest.main()
