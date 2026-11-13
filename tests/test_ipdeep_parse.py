"""IPDEEP 代理返回解析的单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.net.ipdeep import parse_proxy_payload  # noqa: E402


class TestParseProxyPayload(unittest.TestCase):
    def test_pascal_case_list(self):
        got = parse_proxy_payload(
            '[{"Ip":"51.1.2.3","Port":8080,"Username":"u1","Password":"p1",'
            '"CountryCode":"SG"}]')
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].url, "http://u1:p1@51.1.2.3:8080")
        self.assertEqual(got[0].country, "SG")

    def test_nested_data_camel(self):
        got = parse_proxy_payload(
            '{"code":0,"data":[{"ip":"51.1.2.4","port":9000,'
            '"username":"u2","password":"p2"}]}')
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].url, "http://u2:p2@51.1.2.4:9000")

    def test_colon_lines(self):
        got = parse_proxy_payload("51.1.2.5:7000:user5:pass5\n51.1.2.8:8000")
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0].url, "http://user5:pass5@51.1.2.5:7000")
        self.assertEqual(got[1].url, "http://51.1.2.8:8000")

    def test_at_style_line(self):
        got = parse_proxy_payload("user6:pass6@51.1.2.6:6000")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].url, "http://user6:pass6@51.1.2.6:6000")

    def test_list_of_strings(self):
        got = parse_proxy_payload(["1.1.1.1:1111", "2.2.2.2:2222:u:p"])
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1].url, "http://u:p@2.2.2.2:2222")

    def test_empty(self):
        self.assertEqual(parse_proxy_payload(""), [])
        self.assertEqual(parse_proxy_payload(None), [])
        self.assertEqual(parse_proxy_payload("no proxy here"), [])


if __name__ == "__main__":
    unittest.main()
