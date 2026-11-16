"""一条一用代理论：账本、一次性取用、模板续期。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.net.proxy import (
    ProxyLedger,
    ProxyPool,
    acquire_once,
    append_to_file,
    extend_from_template,
    parse_line,
    render_proxy,
)

LINES = [
    "gate.ipdeep.com:8080:u1-session-aaaaaaaa-sessiontime-5:p",
    "gate.ipdeep.com:8080:u2-session-bbbbbbbb-sessiontime-5:p",
    "gate.ipdomain.com:8080:u3-session-cccccccc-sessiontime-5:p",
]


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.ledger = ProxyLedger(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reserve_then_used(self):
        px = parse_line(LINES[0])
        self.assertFalse(self.ledger.is_used(px.raw))
        self.ledger.reserve(px, env="acct1")
        self.assertTrue(self.ledger.is_used(px.raw))
        st = self.ledger.stats()
        self.assertEqual(st["total"], 1)
        self.assertEqual(st["by_note"].get("reserved"), 1)

    def test_mark_updates_note(self):
        px = parse_line(LINES[0])
        self.ledger.reserve(px)
        self.ledger.mark(px, env="acct2", ip="1.2.3.4", note="ok")
        entry = self.ledger._read()["entries"][px.raw]
        self.assertEqual(entry["note"], "ok")
        self.assertEqual(entry["ip"], "1.2.3.4")
        self.assertEqual(entry["env"], "acct2")

    def test_remaining_counts_unused(self):
        px = parse_line(LINES[0])
        self.ledger.reserve(px)
        self.assertEqual(self.ledger.remaining([p.raw for p in
                                                map(parse_line, LINES)]), 2)

    def test_prune_drops_old(self):
        px = parse_line(LINES[0])
        self.ledger.reserve(px)
        # 把时间改到很久以前
        data = self.ledger._read()
        data["entries"][px.raw]["used_at"] = 0
        self.ledger._write(data)
        self.assertEqual(self.ledger.prune(keep_hours=1), 1)
        self.assertEqual(self.ledger.stats()["total"], 0)

    def test_survives_corrupt_file(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertFalse(self.ledger.is_used("whatever"))
        self.ledger.reserve(parse_line(LINES[0]))
        self.assertEqual(self.ledger.stats()["total"], 1)


class TestAcquireOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        f = d / "proxies.txt"
        f.write_text("\n".join(LINES) + "\n", encoding="utf-8")
        self.pool = ProxyPool.from_file(f)
        self.ledger = ProxyLedger(d / "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_call_returns_new_proxy(self):
        seen = []
        for _ in range(3):
            px = acquire_once(self.pool, self.ledger, check=False)
            self.assertIsNotNone(px)
            seen.append(px.raw)
        self.assertEqual(len(set(seen)), 3, "同一轮里不许重复取到同一条")

    def test_exhausted_returns_none(self):
        for _ in range(3):
            acquire_once(self.pool, self.ledger, check=False)
        self.assertIsNone(acquire_once(self.pool, self.ledger, check=False))

    def test_dead_proxy_is_consumed_not_returned(self):
        px = acquire_once(self.pool, self.ledger, check=False)
        self.ledger.mark(px, note="dead")
        nxt = acquire_once(self.pool, self.ledger, check=False)
        self.assertNotEqual(px.raw, nxt.raw)


class TestTemplate(unittest.TestCase):
    TEMPLATE = "gate.ipdeep.com:8080:{user}-session-{session}-sessiontime-{ttl}:{password}"

    def test_render_replaces_all_placeholders(self):
        raw = render_proxy(self.TEMPLATE, region="us", user="acc-res-country-us",
                           password="pw", ttl=5, session="deadbeef")
        self.assertEqual(
            raw,
            "gate.ipdeep.com:8080:acc-res-country-us-session-deadbeef-sessiontime-5:pw")
        px = parse_line(raw)
        self.assertEqual(px.host, "gate.ipdeep.com")
        self.assertEqual(px.port, 8080)
        self.assertEqual(px.session, "deadbeef")
        self.assertEqual(px.sticky_minutes, 5)

    def test_extend_unique_sessions(self):
        raws = extend_from_template(
            template=self.TEMPLATE, user="u", password="p", ttl=5,
            count=10, region="us", verify=False)
        self.assertEqual(len(raws), 10)
        self.assertEqual(len(set(raws)), 10)
        sessions = {parse_line(r).session for r in raws}
        self.assertEqual(len(sessions), 10)
        self.assertNotIn(None, sessions)


class TestAppendFile(unittest.TestCase):
    def test_dedupe_on_append(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.txt"
            f.write_text(LINES[0] + "\n", encoding="utf-8")
            added = append_to_file(f, [LINES[0], LINES[1]])
            self.assertEqual(added, 1)
            self.assertEqual(len(f.read_text(encoding="utf-8").splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
