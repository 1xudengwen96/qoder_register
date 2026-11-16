"""登录链接获取：剪贴板 / 浏览器历史 / 新鲜度过滤。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.claim.ui_login import DEEP_LINK_RE, LINK_RE, from_browser_history

LINK = ("https://www.qoder.com/device/selectAccounts?nonce=abc123&challenge=XYZ"
        "&challenge_method=S256&redirect_uri=qoder%3A%2F%2Fx%2Flogin-success"
        "&machine_token=P1gTOKEN&machine_id=1111")


def _chrome_now() -> int:
    """当前时间 → Chrome 的 1601 纪元微秒。"""
    return int((time.time() + 11644473600) * 1_000_000)


def _make_history(root: Path, urls: list[tuple[str, int]]) -> Path:
    prof = root / "Google" / "Chrome" / "User Data" / "Default"
    prof.mkdir(parents=True, exist_ok=True)
    db = prof / "History"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE urls (url TEXT, last_visit_time INTEGER)")
    con.executemany("INSERT INTO urls VALUES (?, ?)", urls)
    con.commit()
    con.close()
    return db


class TestRegex(unittest.TestCase):
    def test_link_regex(self):
        m = LINK_RE.search("随便什么东西 " + LINK + " 后面还有字")
        self.assertIsNotNone(m)
        self.assertTrue(m.group(0).startswith("https://www.qoder.com/device/selectAccounts?"))

    def test_deep_link_regex(self):
        m = DEEP_LINK_RE.search('跳到 qoder://aicoding/login-success?code=abc&state=1 完成')
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1).startswith("qoder://"))

    def test_deep_link_ide_scheme(self):
        m = DEEP_LINK_RE.search("qoder-ide://aicoding.aicoding-agent/login-success?code=1")
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1).startswith("qoder-ide://"))


class TestBrowserHistory(unittest.TestCase):
    def test_reads_recent_link(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_history(root, [(LINK, _chrome_now())])
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)}, clear=False):
                got = from_browser_history()
        self.assertEqual(got, LINK)

    def test_ignores_old_link(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old = _chrome_now() - 7200 * 1_000_000      # 两小时前
            _make_history(root, [(LINK, old)])
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)}, clear=False):
                self.assertEqual(from_browser_history(max_age_sec=60), "")

    def test_since_filters_replayed_nonce(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_history(root, [(LINK, _chrome_now())])
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)}, clear=False):
                # since 设成 10 分钟之后：这条历史链接应被判定为“上一轮的”，不许复用
                got = from_browser_history(since=time.time() + 600)
        self.assertEqual(got, "")

    def test_picks_newest_of_several(self):
        older = LINK.replace("nonce=abc123", "nonce=OLD")
        newer = LINK.replace("nonce=abc123", "nonce=NEW")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            now = _chrome_now()
            _make_history(root, [(older, now - 60 * 1_000_000),
                                 (newer, now)])
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)}, clear=False):
                got = from_browser_history()
        self.assertIn("nonce=NEW", got)

    def test_no_history_file(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": d}, clear=False):
                self.assertEqual(from_browser_history(), "")


if __name__ == "__main__":
    unittest.main()
