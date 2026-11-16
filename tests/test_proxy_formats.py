"""代理格式解析 + 比特浏览器代理字段翻译。

覆盖的真实事故：代理商导出的 ``host:port:user:pass`` 被 ``urlparse`` 当成
非法端口，一行抛异常把整批 50 条代理全废掉。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.net.proxy import Proxy, ProxyPool, parse_line
from app.register.bitbrowser import build_proxy_config

MOOPROXY = ("hk.mooproxy.net:55688:web_a1p6PcWl:"
            "N0GJCC6x3H_country-cn_session-a77186f2c4d36323_time-30")


class TestParseLine(unittest.TestCase):
    def test_four_part_colon(self):
        px = parse_line(MOOPROXY)
        self.assertIsNotNone(px)
        self.assertEqual(px.host, "hk.mooproxy.net")
        self.assertEqual(px.port, 55688)
        self.assertEqual(px.username, "web_a1p6PcWl")
        self.assertEqual(px.password,
                         "N0GJCC6x3H_country-cn_session-a77186f2c4d36323_time-30")
        self.assertEqual(px.scheme, "http")
        self.assertEqual(px.region, "cn")
        self.assertEqual(px.session, "a77186f2c4d36323")
        self.assertEqual(px.sticky_minutes, 30)
        self.assertTrue(px.raw.startswith("http://web_a1p6PcWl:"))
        self.assertIn("@hk.mooproxy.net:55688", px.raw)

    def test_four_part_with_region_tag(self):
        px = parse_line("hk.mooproxy.net:55688:user:pass#hk")
        self.assertIsNotNone(px)
        self.assertEqual(px.region, "hk")      # 显式标签优先于账号串推断

    def test_user_pass_at_host(self):
        px = parse_line("user:p%40ss@1.2.3.4:8080")
        self.assertIsNotNone(px)
        self.assertEqual((px.host, px.port), ("1.2.3.4", 8080))
        self.assertEqual(px.username, "user")
        self.assertEqual(px.password, "p%40ss")

    def test_full_url_http(self):
        px = parse_line("http://u:p@9.9.9.9:3128#uk")
        self.assertIsNotNone(px)
        self.assertEqual(px.scheme, "http")
        self.assertEqual(px.region, "uk")
        self.assertEqual(px.raw, "http://u:p@9.9.9.9:3128")

    def test_socks5(self):
        px = parse_line("socks5://u:p@5.6.7.8:1080#jp")
        self.assertIsNotNone(px)
        self.assertEqual(px.scheme, "socks5")
        self.assertEqual(build_proxy_config(px)["proxyType"], "socks5")

    def test_host_port_only(self):
        px = parse_line("1.2.3.4:8080")
        self.assertIsNotNone(px)
        self.assertEqual((px.host, px.port), ("1.2.3.4", 8080))
        self.assertIsNone(px.username)
        self.assertEqual(px.raw, "http://1.2.3.4:8080")

    def test_password_with_colon(self):
        px = parse_line("h.example.com:9000:user:pa:ss:word")
        self.assertIsNotNone(px)
        self.assertEqual(px.password, "pa:ss:word")

    def test_garbage_returns_none_without_raising(self):
        for bad in ("", "   ", "#only-region", "nonsense", "h:port:u:p",
                    "//comment", "h:99999999:u:p"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_line(bad))


class TestProxyPoolFile(unittest.TestCase):
    def test_mixed_file_does_not_explode(self):
        body = "\n".join([
            "# 注释行",
            MOOPROXY,
            "坏行 without port",
            "1.2.3.4:8080",
            "",
            "u:p@5.6.7.8:1080#us",
        ])
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "proxies.txt"
            f.write_text(body, encoding="utf-8")
            pool = ProxyPool.from_file(f)
        self.assertEqual(len(pool.proxies), 3)
        self.assertGreaterEqual(pool.skipped, 1)
        self.assertIn("格式跳过", pool.summary())

    def test_missing_file_is_empty(self):
        pool = ProxyPool.from_file("data/definitely-not-here.txt")
        self.assertEqual(pool.proxies, [])

    def test_shipped_sticky_list(self):
        p = Path("proxies-cn-sticky.txt")
        if not p.exists():
            self.skipTest("仓库里没有 proxies-cn-sticky.txt")
        pool = ProxyPool.from_file(p)
        self.assertGreater(len(pool.proxies), 10)
        first = pool.proxies[0]
        self.assertEqual(first.host, "hk.mooproxy.net")
        self.assertEqual(first.port, 55688)
        self.assertIn(first.region, ("cn", "hk", "us", "jp"))


class TestBitBrowserProxyFields(unittest.TestCase):
    def test_none_means_direct(self):
        self.assertEqual(build_proxy_config(None),
                         {"proxyMethod": 3, "proxyType": "noproxy"})

    def test_proxy_object(self):
        px = parse_line("hk.mooproxy.net:55688:web:secret")
        cfg = build_proxy_config(px)
        self.assertEqual(cfg["proxyMethod"], 2)
        self.assertEqual(cfg["proxyType"], "http")
        self.assertEqual(cfg["host"], "hk.mooproxy.net")
        self.assertEqual(cfg["port"], "55688")
        self.assertEqual(cfg["proxyUserName"], "web")
        self.assertEqual(cfg["proxyPassword"], "secret")
        self.assertFalse(cfg["enableSocks5Udp"])

    def test_raw_string_with_four_parts(self):
        cfg = build_proxy_config(MOOPROXY)
        self.assertEqual(cfg["host"], "hk.mooproxy.net")
        self.assertEqual(cfg["port"], "55688")

    def test_socks5_flag(self):
        cfg = build_proxy_config("socks5://u:p@1.1.1.1:1080")
        self.assertEqual(cfg["proxyType"], "socks5")
        self.assertTrue(cfg["enableSocks5Udp"])

    def test_proxy_method_dataclass_helper(self):
        px = parse_line("1.2.3.4:8080")
        self.assertEqual(px.bitbrowser_proxy()["proxyType"], "http")


if __name__ == "__main__":
    unittest.main()
