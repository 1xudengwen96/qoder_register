"""领取段两个修复的回归测试（全 mock，不碰真实客户端 / 比特浏览器）：

1. ``Claimer.wait_new_token`` 必须走 ``harvest()`` 全链路降级 ——
   0.4.x 客户端 token 在 ``auth.v1.dat``（DPAPI），``harvest_from_file``
   只认 0.3.x 明文 JSON，直接调它在 0.4.x 上永远拿不到 token。
2. ``drive_login`` 状态机读 ``deepLink``（与 ``_JS_PAGE_STATE`` 返回键一致）——
   之前误写 ``st.get("deep")``，导致 qoder:// 回跳永远抓不到。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.claim.claimer import Claimer
from app.claim.token_harvester import QoderToken
from app.claim.ui_login import drive_login


# ============================================================ 修复 1
class _FakeHarvester:
    """0.4.x 场景：machine_token.json 不存在（重置已清），token 走 safe_storage。"""

    def __init__(self, token: QoderToken | None) -> None:
        self._tok = token
        self.harvest_calls = 0
        self.file_calls = 0

    def harvest(self) -> QoderToken | None:
        self.harvest_calls += 1
        return self._tok

    def harvest_from_file(self) -> QoderToken | None:
        self.file_calls += 1
        return None


def _claimer(tmp: str) -> Claimer:
    return Claimer(client_path="C:/__nope__/Qoder.exe",
                   account_data_dir=tmp, relogin_timeout=2)


class TestWaitNewToken(unittest.TestCase):
    def test_token_via_full_harvest_on_04x(self):
        """0.4.x：明文文件不存在，也必须等到 token（safe_storage 路径）。"""
        tok = QoderToken(token="NEW", source="safe_storage")
        fake = _FakeHarvester(tok)
        with tempfile.TemporaryDirectory() as tmp:
            c = _claimer(tmp)
            c.harvester = fake
            got = c.wait_new_token("OLD", timeout=6)
        self.assertIsNotNone(got)
        self.assertEqual(got.token, "NEW")
        self.assertGreaterEqual(fake.harvest_calls, 1)

    def test_same_token_times_out_to_current(self):
        """token 与旧值相同（同号重登）→ 超时后返回当前 token，不返回 None。"""
        fake = _FakeHarvester(QoderToken(token="SAME", source="machine_token"))
        with tempfile.TemporaryDirectory() as tmp:
            c = _claimer(tmp)
            c.harvester = fake
            got = c.wait_new_token("SAME", timeout=1)
        self.assertIsNotNone(got)
        self.assertEqual(got.token, "SAME")

    def test_no_token_returns_none(self):
        fake = _FakeHarvester(None)
        with tempfile.TemporaryDirectory() as tmp:
            c = _claimer(tmp)
            c.harvester = fake
            got = c.wait_new_token("OLD", timeout=1)
        self.assertIsNone(got)


# ============================================================ 修复 2
class _FakePage:
    """模拟登录页：selectAccounts 页面上挂着 qoder:// 回跳链接。"""

    def __init__(self) -> None:
        self.visited: list[str] = []
        self.closed = False

    def goto(self, url: str, wait_ms: int = 0) -> str:
        self.visited.append(url)
        return url

    def eval(self, expression: str, await_promise: bool = False):
        if "deepLink" in expression:      # _JS_PAGE_STATE
            return {
                "url": "https://www.qoder.com/device/selectAccounts?nonce=1",
                "text": "Select account | Pro Trial | a@b.c 0 / 300 (0% used) 300 left",
                "inputs": [],
                "buttons": ["Continue"],
                "deepLink": "qoder://aicoding/login-success?code=abc&state=1",
            }
        return None

    def text(self) -> str:
        return ("Plan Credits\n0 / 300 (0% used) 300 left\n"
                "| Pro Trial | a@b.c")

    def shot(self, path: str) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"png")
        return path

    def close(self) -> None:
        self.closed = True


class TestDriveLoginDeepLink(unittest.TestCase):
    def test_deep_link_captured_and_credits_read(self):
        from app.register import bb_cdp
        from app.register.bb_cdp import BBLaunch

        page = _FakePage()
        launch = BBLaunch(env_id="env1", env_name="n", endpoint="http://127.0.0.1:9",
                          created=True)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(bb_cdp, "open_for_register",
                                   return_value=launch), \
                 mock.patch.object(bb_cdp, "close_env",
                                   return_value=True) as m_close, \
                 mock.patch("app.register.cdp_page.new_page",
                           return_value=page):
                out = drive_login(
                    None,
                    "https://www.qoder.com/device/selectAccounts?nonce=1",
                    "a@b.c", "pw-secret",
                    env_name="t", region="us", timeout=10,
                    artifacts_dir=str(Path(tmp) / "arts"),
                )

        # 回跳链接被正确捕获（deepLink 键名修复的直接断言）
        self.assertTrue(out.ok, out.error)
        self.assertTrue(out.deep_link.startswith("qoder://aicoding/login-success"))
        # 会话内额度读取正常
        self.assertEqual(out.credits, 300)
        self.assertEqual(out.plan, "Pro Trial")
        # 环境收尾
        m_close.assert_called_once()
        self.assertTrue(page.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ============================================================ 修复 3+：
# 设计断裂（orchestrator 领取段无登录动作）与次要问题
from datetime import datetime  # noqa: E402
import os  # noqa: E402

from app.claim.claimer import ClaimResult, apply_credits_fallback  # noqa: E402
from app.claim.credit_page import Credits, parse_usage  # noqa: E402
from app.claim.ui_login import LoginOutcome, make_ui_login  # noqa: E402

LINK = ("https://www.qoder.com/device/selectAccounts?nonce=abc123"
        "&challenge=XYZ")


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def json(self) -> dict:
        return self._p


class TestProbeCreditsExpire(unittest.TestCase):
    """expire 解析：之前只试 naive strptime，带时区/RFC 格式静默落 14 天兑底。"""

    def _probe(self, expire_raw: str) -> datetime | None:
        with tempfile.TemporaryDirectory() as tmp:
            c = _claimer(tmp)
            c.credit_probe_url = "http://x/y"
            with mock.patch("app.claim.claimer.requests.get",
                            return_value=_Resp({"credits": 300, "plan": "Pro Trial",
                                                 "expireAt": expire_raw})):
                return c.probe_credits(QoderToken(token="T"))[2]

    def test_naive_format(self):
        got = self._probe("2026-12-01 10:00:00")
        self.assertEqual(got, datetime(2026, 12, 1, 10, 0, 0))

    def test_iso_utc_z(self):
        got = self._probe("2026-12-01T10:00:00Z")
        want = datetime.fromisoformat("2026-12-01T10:00:00+00:00").astimezone().replace(tzinfo=None)
        self.assertEqual(got, want)
        self.assertIsNone(got.tzinfo)

    def test_iso_offset(self):
        got = self._probe("2026-12-01T10:00:00+08:00")
        want = datetime.fromisoformat("2026-12-01T10:00:00+08:00").astimezone().replace(tzinfo=None)
        self.assertEqual(got, want)

    def test_rfc1123_gmt(self):
        got = self._probe("Tue, 01 Dec 2026 10:00:00 GMT")
        from email.utils import parsedate_to_datetime

        want = parsedate_to_datetime("Tue, 01 Dec 2026 10:00:00 GMT").astimezone().replace(tzinfo=None)
        self.assertEqual(got, want)

    def test_garbage_falls_to_none(self):
        self.assertIsNone(self._probe("not-a-date"))


class TestCreditsProperty(unittest.TestCase):
    """credits 属性：left=0 是真实值，不许误报成总量 300。"""

    def test_left_zero_is_zero(self):
        c = parse_usage("0 / 300 (100% used) 0 left")
        self.assertEqual(c.total, 300)
        self.assertEqual(c.left, 0)
        self.assertEqual(c.credits, 0)

    def test_left_normal(self):
        c = parse_usage("50 / 300 (16% used) 250 left")
        self.assertEqual(c.credits, 250)

    def test_loose_fallback_is_total(self):
        c = parse_usage("300 Credits")
        self.assertEqual(c.credits, 300)

    def test_no_read_is_zero(self):
        self.assertEqual(Credits().credits, 0)


@unittest.skipIf(os.name != "nt", "BCrypt 兜底仅 Windows")
class TestBcryptGcmStatus(unittest.TestCase):
    """_bcrypt_gcm 必须检查 BCryptDecrypt 返回值：解密失败显式返空。"""

    def test_decrypt_failure_returns_empty(self):
        from app.claim.token_harvester import _bcrypt_gcm

        # 随机 payload 的 tag 必然不匹配 → ntstatus 非 0
        got = _bcrypt_gcm(b"k" * 32, b"\x00" * 44)
        self.assertEqual(got, b"")


class TestMakeUiLogin(unittest.TestCase):
    """半自动登录回调工厂（cli 与 orchestrator 共用）。"""

    def test_full_flow_state_and_deep_link(self):
        out = LoginOutcome(True, deep_link="qoder://aicoding/login-success?code=1",
                          credits=300, plan="Pro Trial")
        px = object()
        with mock.patch("app.claim.ui_login.wait_link",
                        return_value=(LINK, "clipboard")), \
             mock.patch("app.claim.ui_login.drive_login",
                        return_value=out) as m_drive, \
             mock.patch("app.claim.ui_login.dispatch_deep_link",
                        return_value=True) as m_disp, \
             mock.patch("app.net.proxy.ProxyPool") as m_pool, \
             mock.patch("app.net.proxy.ProxyLedger") as m_ledger_cls, \
             mock.patch("app.net.proxy.acquire_once", return_value=px):
            ui, state = make_ui_login({}, "a@b.c", "pw", env_name="t",
                                      link_timeout=5, login_timeout=10)
            ok = ui(None)

        self.assertTrue(ok)
        m_drive.assert_called_once()
        self.assertEqual(m_drive.call_args.args[2], "a@b.c")  # 位置参数：cfg, link, email
        m_disp.assert_called_once_with("qoder://aicoding/login-success?code=1")
        m_pool.from_file.assert_called_once()
        m_ledger_cls.return_value.mark.assert_called_once_with(
            px, env=out.env_id, note="login")
        # state 给额度兜底用
        self.assertEqual(state["credits"], 300)
        self.assertEqual(state["plan"], "Pro Trial")

    def test_no_link_returns_false(self):
        with mock.patch("app.claim.ui_login.wait_link", return_value=("", "")):
            ui, state = make_ui_login({}, "a@b.c", "pw", link_timeout=1)
        self.assertFalse(ui(None))
        self.assertEqual(state, {})

    def test_login_failed_returns_false(self):
        out = LoginOutcome(False, error="captcha")
        with mock.patch("app.claim.ui_login.wait_link",
                        return_value=(LINK, "clipboard")), \
             mock.patch("app.claim.ui_login.drive_login", return_value=out), \
             mock.patch("app.net.proxy.ProxyPool"), \
             mock.patch("app.net.proxy.ProxyLedger"), \
             mock.patch("app.net.proxy.acquire_once", return_value=object()):
            ui, state = make_ui_login({}, "a@b.c", "pw", link_timeout=5)
        self.assertFalse(ui(None))
        self.assertEqual(state, {})


class TestApplyCreditsFallback(unittest.TestCase):
    """额度兜底：登录会话优先 → 账号页重读，返回来源标签。"""

    def test_already_has_credits_no_fallback(self):
        cres = ClaimResult(True, credits=300)
        with mock.patch("app.claim.credit_page.read_credits") as m_read:
            self.assertEqual(apply_credits_fallback(cres, {}, {}), "")
        m_read.assert_not_called()

    def test_login_session_credits(self):
        cres = ClaimResult(True, credits=0)
        src = apply_credits_fallback(cres, {"credits": 300, "plan": "Pro Trial"}, {})
        self.assertEqual(src, "login-session")
        self.assertEqual(cres.credits, 300)
        self.assertEqual(cres.plan, "Pro Trial")

    def test_account_page_credits(self):
        cres = ClaimResult(True, credits=0, plan="pro_trial")
        cred = Credits(used=50, total=300, left=250, percent=16,
                       plan="Pro Trial", source="usage-page")
        state: dict = {}
        with mock.patch("app.claim.credit_page.read_credits", return_value=cred):
            src = apply_credits_fallback(cres, state, {}, env_name="t")
        self.assertEqual(src, "account-page")
        self.assertEqual(cres.credits, 250)
        self.assertEqual(cres.plan, "Pro Trial")
        self.assertEqual(state["_page"]["left"], 250)

    def test_account_page_read_fails_keeps_zero(self):
        cres = ClaimResult(True, credits=0)
        cred = Credits(source="error:Timeout")
        state: dict = {}
        with mock.patch("app.claim.credit_page.read_credits", return_value=cred):
            src = apply_credits_fallback(cres, state, {}, env_name="t")
        self.assertEqual(src, "account-page")
        self.assertEqual(cres.credits, 0)


class TestOrchestratorClaimWiring(unittest.TestCase):
    """orchestrator 领取段接线：claim.login_mode=cdp 时传半自动登录回调，
    默认 inherit 保持原行为（ui_login=None）。"""

    _PATCHES = ("build_mail_layer", "ProxyPool", "build_solver",
                "build_fingerprint_engine", "BrowserRegistrar",
                "QoderIdentityReset", "Claimer")

    def _pipeline(self, cfg: dict) -> tuple:
        from app.orchestrator import Pipeline

        mocks = {k: mock.MagicMock() for k in self._PATCHES}
        with mock.patch.multiple("app.orchestrator", **mocks), \
                mock.patch("app.secrets.export_env"):
            return Pipeline(cfg), mocks

    def test_init_reads_claim_login_mode(self):
        pipe, mocks = self._pipeline({"claim": {"login_mode": "cdp"}})
        self.assertEqual(pipe.claim_login_mode, "cdp")
        self.assertEqual(mocks["Claimer"].call_args.kwargs["login_mode"], "cdp")

        pipe, mocks = self._pipeline({})
        self.assertEqual(pipe.claim_login_mode, "inherit")
        self.assertEqual(mocks["Claimer"].call_args.kwargs["login_mode"], "inherit")

    def test_cdp_mode_passes_ui_login(self):
        from app.orchestrator import Pipeline

        p = Pipeline.__new__(Pipeline)
        p.cfg = {"claim": {"login_mode": "cdp"}, "region": "us"}
        p.claim_login_mode = "cdp"
        p.claimer = mock.MagicMock()
        p.claimer.perform.return_value = ClaimResult(True, credits=0)
        with mock.patch("app.claim.ui_login.make_ui_login") as mk, \
             mock.patch("app.claim.claimer.apply_credits_fallback") as fb:
            mk.return_value = (mock.sentinel.ui, {})
            fb.return_value = "account-page"
            cres = p._claim_with_fallback("a@b.c", "pw", 7)

        self.assertTrue(cres.ok)
        mk.assert_called_once()
        # 一一对应：领取登录复用注册环境 acct{id}（不再是 login_acct{id}）
        self.assertEqual(mk.call_args.kwargs["env_name"], "acct7")
        p.claimer.perform.assert_called_once_with("a@b.c", "pw",
                                                  ui_login=mock.sentinel.ui)
        fb.assert_called_once()

    def test_inherit_mode_keeps_none(self):
        from app.orchestrator import Pipeline

        p = Pipeline.__new__(Pipeline)
        p.cfg = {}
        p.claim_login_mode = "inherit"
        p.claimer = mock.MagicMock()
        p.claimer.perform.return_value = ClaimResult(True, credits=300)
        with mock.patch("app.claim.ui_login.make_ui_login") as mk, \
             mock.patch("app.claim.claimer.apply_credits_fallback") as fb:
            cres = p._claim_with_fallback("a@b.c", "pw", 7)

        self.assertTrue(cres.ok)
        mk.assert_not_called()
        p.claimer.perform.assert_called_once_with("a@b.c", "pw", ui_login=None)
        # 已有额度时不走兜底
        fb.assert_not_called()
