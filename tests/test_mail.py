"""邮箱层单元测试：地址派生、tag 风险判定、iCloud 转发匹配、调度策略。

运行：
    set PYTHONPATH=D:\\qoder_register;D:\\qoder_register\\.pylibs
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from app.mail.aggregator import MailAggregator
from app.mail.base import MailIdentity, MailMessage, ProviderHealth
from app.mail.hotmail import HotmailProvider
from app.mail.icloud import ICloudRelayProvider
from app.mail.outlook import OutlookTagProvider, split_address, strip_tag

# 沙箱禁写系统临时目录，清理时 chmod 也被拒；用自己的目录，不主动删除
TMP_ROOT = Path(__file__).resolve().parent / "_tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)


def new_dir() -> Path:
    p = TMP_ROOT / f"t_{uuid.uuid4().hex[:8]}"
    p.mkdir(parents=True, exist_ok=True)
    return p


class TestOutlookTag(unittest.TestCase):
    def test_split_and_strip(self):
        self.assertEqual(split_address("abc+q001@outlook.com"), ("abc", "q001"))
        self.assertEqual(strip_tag("abc+q001@outlook.com"), "abc@outlook.com")
        self.assertEqual(strip_tag("abc@outlook.com"), "abc@outlook.com")

    def test_acquire_generates_tags(self):
        p = OutlookTagProvider(
            [{"user": "main1@outlook.com", "password": "x"}],
            tag_prefix="q", max_tags_per_account=3,
        )
        idents = p.acquire(3)
        self.assertEqual(len(idents), 3)
        self.assertEqual(idents[0].address, "main1+q000@outlook.com")
        self.assertEqual(idents[2].address, "main1+q002@outlook.com")
        self.assertEqual(idents[0].provider, "outlook_tag")

    def test_acquire_skips_taken(self):
        p = OutlookTagProvider(
            [{"user": "main1@outlook.com", "password": "x"}],
            max_tags_per_account=2,
            taken_addresses={"main1+q000@outlook.com"},
        )
        got = p.acquire(1)
        self.assertEqual(got[0].address, "main1+q001@outlook.com")

    def test_normalized_failure_detection(self):
        self.assertTrue(OutlookTagProvider.looks_normalized_failure("Email already registered"))
        self.assertTrue(OutlookTagProvider.looks_normalized_failure("该邮箱已注册"))
        self.assertFalse(OutlookTagProvider.looks_normalized_failure("密码强度不足"))

    def test_health_counts(self):
        p = OutlookTagProvider([{"user": "a@outlook.com", "password": "x"}],
                               max_tags_per_account=2)
        self.assertEqual(p.health().remaining, 3)
        p.acquire(1)
        self.assertEqual(p.health().remaining, 2)


class TestHotmail(unittest.TestCase):
    def test_only_microsoft_domains(self):
        d = new_dir()
        f = d / "a.csv"
        f.write_text(
            "email,password,imap_host\n"
            "x@hotmail.com,p1,outlook.office365.com\n"
            "y@gmail.com,p2,imap.gmail.com\n"
            "z@outlook.com,p3,\n",
            encoding="utf-8",
        )
        p = HotmailProvider(f)
        addrs = [i.address for i in p.acquire(10)]
        self.assertIn("x@hotmail.com", addrs)
        self.assertIn("z@outlook.com", addrs)
        self.assertNotIn("y@gmail.com", addrs)
        self.assertEqual(len(addrs), 2)

    def test_missing_file(self):
        p = HotmailProvider("D:/no/such/file.csv")
        self.assertEqual(p.acquire(1), [])
        self.assertFalse(p.health().ok)


class TestICloudRelay(unittest.TestCase):
    def test_load_and_match(self):
        d = new_dir()
        f = d / "relay.txt"
        f.write_text(
            "# 注释行\n"
            "a1b2c3@privaterelay.appleid.com\n"
            "d4e5f6@privaterelay.appleid.com  # 行内注释\n"
            "not-an-address\n",
            encoding="utf-8",
        )
        p = ICloudRelayProvider(
            f, aggregator_imap={"user": "agg@outlook.com", "password": "x"},
            quota_total=10,
        )
        self.assertEqual(len(p.addresses), 2)
        idents = p.acquire(2)
        self.assertEqual(idents[0].address, "a1b2c3@privaterelay.appleid.com")
        self.assertEqual(p.health().remaining, 0)

    def test_recipient_match(self):
        p = ICloudRelayProvider("D:/no/such.txt",
                                aggregator_imap={"user": "a", "password": "b"})
        ident = MailIdentity(address="a1b2c3@privaterelay.appleid.com",
                             provider="icloud_relay")
        matcher = p.match_by_recipient(ident)
        hit = MailMessage(
            uid="1", mailbox="INBOX",
            to=["a1b2c3@privaterelay.appleid.com"],
            sender="noreply@qoder.com", subject="Verify",
            body_text="click", body_html="",
        )
        miss = MailMessage(
            uid="2", mailbox="INBOX", to=["other@privaterelay.appleid.com"],
            sender="x", subject="Verify", body_text="click", body_html="",
        )
        self.assertTrue(matcher(hit))
        self.assertFalse(matcher(miss))


class TestMailMessage(unittest.TestCase):
    def test_confirm_link_ranking(self):
        msg = MailMessage(
            uid="1", mailbox="INBOX", to=["a@b.com"], sender="qoder",
            subject="Verify your email",
            body_text=(
                "Unsubscribe: https://qoder.com/unsubscribe?id=1 "
                "Privacy: https://qoder.com/privacy "
                "Confirm: https://qoder.com/api/auth/verify?token=abc123"
            ),
            body_html="",
        )
        self.assertEqual(
            msg.confirm_link(("qoder.com",)),
            "https://qoder.com/api/auth/verify?token=abc123",
        )

    def test_no_link(self):
        msg = MailMessage(uid="1", mailbox="INBOX", to=[], sender="",
                          subject="", body_text="no links here", body_html="")
        self.assertIsNone(msg.confirm_link())


class _StubProvider:
    def __init__(self, name: str, count: int) -> None:
        self.name = name
        self._left = count
        self._total = count

    def acquire(self, n: int = 1):
        out = []
        for i in range(min(n, self._left)):
            out.append(MailIdentity(address=f"u{i}@{self.name}.com", provider=self.name))
        self._left -= len(out)
        return out

    def health(self):
        return ProviderHealth(self.name, self._left, self._total, self._left > 0)

    def wait_for_mail(self, *a, **k):
        return None


class TestAggregator(unittest.TestCase):
    def test_weighted(self):
        a = MailAggregator(
            {"x": _StubProvider("x", 10), "y": _StubProvider("y", 10)},
            strategy="weighted", weights={"x": 70, "y": 30},
        )
        got = a.acquire(10)
        self.assertEqual(len(got), 10)
        xs = sum(1 for g in got if g.provider == "x")
        self.assertGreater(xs, 5)

    def test_strict_priority_drains_first(self):
        a = MailAggregator(
            {"fast": _StubProvider("fast", 2), "slow": _StubProvider("slow", 10)},
            strategy="strict_priority", strict_order=["fast", "slow"],
        )
        got = a.acquire(5)
        self.assertEqual([g.provider for g in got],
                         ["fast", "fast", "slow", "slow", "slow"])

    def test_health_sorted(self):
        a = MailAggregator(
            {"x": _StubProvider("x", 1), "y": _StubProvider("y", 5)},
            strategy="weighted", weights={"x": 10, "y": 90},
        )
        self.assertEqual(a.health()[0].name, "y")


if __name__ == "__main__":
    unittest.main(verbosity=2)
