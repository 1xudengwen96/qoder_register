"""iCloud 隐藏地址 → QQ 聚合箱 的匹配与收码规则测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.mail.base import MailMessage  # noqa: E402
from app.mail.relay_qq import (  # noqa: E402
    ICloudRelayQQProvider,
    RelayMatchRules,
    RelayPool,
    guess_code,
    looks_like_code_mail,
    recipient_candidates,
    recipient_hits,
)

QODER_MAIL = """From: Qoder <no-reply@qoder.com>
To: relay.one@privaterelay.appleid.com
"""


def mk(to: list[str], body: str = "", subject: str = "Verify your Email with Qoder",
       sender: str = "Qoder <no-reply@qoder.com>",
       raw_headers: str = "") -> MailMessage:
    return MailMessage(uid="1", mailbox="INBOX", to=to, sender=sender,
                       subject=subject, body_text=body, body_html=body,
                       received_at=None, raw_headers=raw_headers)


class TestRelayPool(unittest.TestCase):
    def test_bare_username_gets_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "relay.txt"
            p.write_text(
                "# 注释行\n"
                "abc123\n"
                "def456@privaterelay.appleid.com  # 行内注释\n"
                "\n"
                "ABC123\n",          # 重复（大小写）
                encoding="utf-8")
            pool = RelayPool(p)
            addrs = pool.load()
        self.assertEqual(len(addrs), 2)
        self.assertEqual(addrs[0], "abc123@privaterelay.appleid.com")
        self.assertEqual(addrs[1], "def456@privaterelay.appleid.com")

    def test_missing_file(self):
        pool = RelayPool(Path("nope/never.txt"))
        self.assertEqual(pool.load(), [])


class TestRecipient(unittest.TestCase):
    def test_header_hit(self):
        msg = mk(["relay.one@privaterelay.appleid.com"],
                 raw_headers=QODER_MAIL)
        self.assertIn("relay.one@privaterelay.appleid.com",
                      recipient_candidates(msg, msg.raw_headers))

    def test_body_only_hit(self):
        body = ("You asked to use relay.two@privaterelay.appleid.com "
                "to sign in to Qoder.")
        msg = mk(["someone-else@example.com"], body=body)
        self.assertIn("relay.two@privaterelay.appleid.com",
                      recipient_candidates(msg, "", RelayMatchRules()))

    def test_body_scan_can_be_disabled(self):
        body = "sent to relay.two@privaterelay.appleid.com"
        msg = mk(["x@example.com"], body=body)
        rules = RelayMatchRules(scan_body_for_recipient=False)
        self.assertNotIn("relay.two@privaterelay.appleid.com",
                         recipient_candidates(msg, "", rules))

    def test_recipient_hits_only_target(self):
        msg = mk(["relay.one@privaterelay.appleid.com"], raw_headers=QODER_MAIL)
        rules = RelayMatchRules()
        self.assertTrue(recipient_hits(msg, "relay.one@privaterelay.appleid.com",
                                       rules))
        self.assertFalse(recipient_hits(msg, "relay.two@privaterelay.appleid.com",
                                        rules))


class TestCodeRules(unittest.TestCase):
    def test_qoder_code(self):
        msg = mk(["relay.one@privaterelay.appleid.com"],
                 body="Your verification code is 376746. It expires in 10 min.")
        rules = RelayMatchRules()
        self.assertTrue(looks_like_code_mail(msg, rules))
        self.assertEqual(guess_code(msg, rules), "376746")

    def test_reject_other_sender(self):
        msg = mk(["relay.one@privaterelay.appleid.com"],
                 body="code 123456", sender="spam@example.com")
        self.assertFalse(looks_like_code_mail(msg, RelayMatchRules()))

    def test_custom_code_regex(self):
        rules = RelayMatchRules(code_regex=r"code[:\s]+([A-Z0-9]{6})")
        msg = mk(["relay.one@privaterelay.appleid.com"],
                 body="Your code: AB12CD")
        self.assertEqual(guess_code(msg, rules), "AB12CD")

    def test_rules_from_config(self):
        rules = RelayMatchRules.from_config({
            "sender_allow": "qoder.com, qoder.sh",
            "subject_contains": ["verify"],
            "lookback": 20,
        })
        self.assertEqual(rules.sender_allow, ("qoder.com", "qoder.sh"))
        self.assertEqual(rules.subject_contains, ("verify",))
        self.assertEqual(rules.lookback, 20)


class TestProvider(unittest.TestCase):
    def _provider(self, folder: Path) -> ICloudRelayQQProvider:
        return ICloudRelayQQProvider(
            relay_addresses_file=folder,
            imap_user="demo@qq.com", imap_password="x",
            rules=RelayMatchRules())

    def test_acquire_skips_taken(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "relay.txt"
            p.write_text("a1\nb2\nc3\n", encoding="utf-8")
            prov = self._provider(p)
            got = prov.acquire(2)
            self.assertEqual([x.address for x in got],
                             ["a1@privaterelay.appleid.com",
                              "b2@privaterelay.appleid.com"])
            self.assertEqual(prov.health().remaining, 1)

    def test_taken_address_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "relay.txt"
            p.write_text("a1\nb2\n", encoding="utf-8")
            prov = ICloudRelayQQProvider(
                relay_addresses_file=p, imap_user="demo@qq.com",
                taken_addresses={"a1@privaterelay.appleid.com"})
            got = prov.acquire(5)
            self.assertEqual([x.address for x in got],
                             ["b2@privaterelay.appleid.com"])


if __name__ == "__main__":
    unittest.main()
