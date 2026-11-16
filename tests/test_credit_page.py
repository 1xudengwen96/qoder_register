"""额度页解析 + 账页计划名。"""

from __future__ import annotations

import unittest

from app.claim.credit_page import claim_hint, parse_plan, parse_usage

USAGE = """
Usage
Plan Credits
Monthly quota and usage for the current plan.
0
 / 0
(0% used)
0 left
Get More Credits
"""

USAGE_HIT = """
Plan Credits
Monthly quota and usage for the current plan.
1,250 / 2,000 (62% used) 750 left
"""

PROFILE = """
Sign in to claim
Download and sign in to the client to claim your free Pro Trial and 300 Credits.
Profile Avatar Name zhang zhang Email 1748906220@qq.com
"""


class TestUsage(unittest.TestCase):
    def test_zero_state(self):
        c = parse_usage(USAGE)
        self.assertEqual((c.total, c.left, c.percent), (0, 0, 0))
        self.assertEqual(c.credits, 0)
        self.assertEqual(c.source, "usage-page")

    def test_real_numbers(self):
        c = parse_usage(USAGE_HIT)
        self.assertEqual(c.used, 1250)
        self.assertEqual(c.total, 2000)
        self.assertEqual(c.left, 750)
        self.assertEqual(c.percent, 62)
        self.assertEqual(c.credits, 750, "持有额度取剩余")

    def test_loose_fallback(self):
        c = parse_usage("Your plan includes 300 Credits")
        self.assertEqual(c.total, 300)
        self.assertEqual(c.source, "usage-page(loose)")

    def test_empty(self):
        c = parse_usage("")
        self.assertEqual(c.total, 0)


class TestPlan(unittest.TestCase):
    def test_free(self):
        self.assertEqual(parse_plan("zhang zhang | Free | 1748906220@qq.com"), "Free")

    def test_pro_trial(self):
        self.assertEqual(parse_plan("Current Plan: Pro Trial, 14 days left"), "Pro Trial")

    def test_unknown(self):
        self.assertEqual(parse_plan("nothing here"), "")


class TestHint(unittest.TestCase):
    def test_client_hint_present(self):
        self.assertTrue(claim_hint(PROFILE))

    def test_hint_absent(self):
        self.assertFalse(claim_hint("Profile Avatar Name zhang zhang"))


if __name__ == "__main__":
    unittest.main()
