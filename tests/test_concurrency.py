"""并发模块单元测试：设备锁、账号队列。"""

from __future__ import annotations

import os
import threading
import time
import unittest
import uuid
from pathlib import Path

from app.concurrent import AccountQueue, DeviceLock, QueueItem

TMP = Path(__file__).resolve().parent / "_tmp"
TMP.mkdir(parents=True, exist_ok=True)


def lock_path() -> Path:
    return TMP / f"lock_{uuid.uuid4().hex[:8]}.lock"


class TestDeviceLock(unittest.TestCase):
    def setUp(self):
        self.lp = lock_path()

    def tearDown(self):
        try:
            self.lp.unlink()
        except Exception:
            pass

    def test_acquire_release(self):
        lk = DeviceLock(self.lp)
        self.assertTrue(lk.acquire())
        self.assertTrue(self.lp.exists())
        lk.release()
        self.assertFalse(self.lp.exists())

    def test_second_holder_blocked(self):
        a = DeviceLock(self.lp)
        b = DeviceLock(self.lp)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire(timeout=0.2))
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_reentrant_same_instance(self):
        lk = DeviceLock(self.lp)
        self.assertTrue(lk.acquire())
        self.assertTrue(lk.acquire())
        lk.release()
        self.assertTrue(self.lp.exists(), "内层释放后锁应仍被持有")
        lk.release()
        self.assertFalse(self.lp.exists())

    def test_stale_lock_takeover(self):
        self.lp.write_text('{"pid": 999999999, "ts": 1}', encoding="utf-8")
        lk = DeviceLock(self.lp, stale_after=1)
        self.assertTrue(lk.acquire(timeout=0))
        lk.release()

    def test_dead_pid_treated_as_stale(self):
        self.lp.write_text(
            f'{{"pid": 999999999, "ts": {time.time()}}}', encoding="utf-8")
        lk = DeviceLock(self.lp, stale_after=99999)
        self.assertTrue(lk.acquire(timeout=0), "进程不存在的锁应视为僵尸")
        lk.release()

    def test_live_pid_not_stolen(self):
        self.lp.write_text(
            f'{{"pid": {os.getpid()}, "ts": {time.time()}}}', encoding="utf-8")
        lk = DeviceLock(self.lp, stale_after=99999)
        # 同一 pid 视为自己持有？不，新实例应认为被别人占（同进程不同实例共享文件）
        got = lk.acquire(timeout=0)
        self.assertFalse(got, f"本进程 pid 的锁不应被子实例抢占，实际 {got}")

    def test_mutual_exclusion_under_contention(self):
        held_max = 0
        active = 0
        guard = threading.Lock()

        def worker():
            nonlocal held_max, active
            lk = DeviceLock(self.lp, stale_after=600)
            for _ in range(20):
                if lk.acquire(timeout=1.0):
                    with guard:
                        active += 1
                        held_max = max(held_max, active)
                    time.sleep(0.001)
                    with guard:
                        active -= 1
                    lk.release()
                else:
                    time.sleep(0.005)

        ts = [threading.Thread(target=worker) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(held_max, 1, f"同时持有最大值应为 1，实际 {held_max}")

    def test_context_manager(self):
        with DeviceLock(self.lp) as lk:
            self.assertTrue(self.lp.exists())
            self.assertIsInstance(lk, DeviceLock)
        self.assertFalse(self.lp.exists())

    def test_context_manager_raises_when_held(self):
        a = DeviceLock(self.lp)
        a.acquire()
        with self.assertRaises(RuntimeError):
            with DeviceLock(self.lp):
                pass
        a.release()


class TestAccountQueue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.db import create_all, init_engine

        cls.db = TMP / "queue_test.db"
        init_engine(f"sqlite:///{cls.db.as_posix()}")
        create_all(drop=True)

    def _seed(self, n: int, prefix: str = "q") -> list[int]:
        from app.db import session_scope
        from app.models import Account, AccountState

        ids = []
        with session_scope() as s:
            for i in range(n):
                a = Account(email=f"{prefix}{uuid.uuid4().hex[:8]}@t.local",
                            password="p", state=AccountState.VERIFIED)
                s.add(a)
                s.flush()
                ids.append(a.id)
        return ids

    def test_claim_returns_item(self):
        self._seed(1, "c1")
        q = AccountQueue("t")
        item = q.claim_next(max_wait=0)
        self.assertIsNotNone(item)
        self.assertIsInstance(item, QueueItem)
        self.assertIn("@t.local", item.email)

    def test_claim_marks_claiming(self):
        from app.db import get_session
        from app.models import Account, AccountState

        ids = self._seed(1, "c2")
        q = AccountQueue("t")
        item = q.claim_next(max_wait=0)
        s = get_session()
        try:
            self.assertEqual(s.get(Account, item.account_id).state, AccountState.CLAIMING)
        finally:
            s.close()

    def test_empty_queue_returns_none(self):
        from app.db import session_scope
        from app.models import Account, AccountState

        with session_scope() as s:
            s.query(Account).delete()
        q = AccountQueue("t")
        self.assertIsNone(q.claim_next(max_wait=0))

    def test_no_duplicate_claims(self):
        from app.db import session_scope
        from app.models import Account

        with session_scope() as s:
            s.query(Account).delete()
        total = 60
        self._seed(total, "dup")

        q = AccountQueue("t")
        got: list[int] = []
        guard = threading.Lock()

        def consumer():
            while True:
                it = q.claim_next(max_wait=0)
                if it is None:
                    return
                with guard:
                    got.append(it.account_id)

        ts = [threading.Thread(target=consumer) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        self.assertEqual(len(got), total, f"应领取 {total} 个，实际 {len(got)}")
        self.assertEqual(len(set(got)), total, "出现重复领取")

    def test_reclaim_stale(self):
        from datetime import datetime, timedelta

        from app.db import session_scope

        from app.models import Account, AccountState

        with session_scope() as s:
            s.query(Account).delete()
            a = Account(email="stale@t.local", password="p",
                        state=AccountState.CLAIMING)
            s.add(a)
            s.flush()
            aid = a.id
            a.updated_at = datetime.now() - timedelta(hours=3)

        q = AccountQueue("t")
        n = q.reclaim_stale(older_than_minutes=30)
        self.assertGreaterEqual(n, 1)

        from app.db import get_session
        s2 = get_session()
        try:
            self.assertEqual(s2.get(Account, aid).state, AccountState.VERIFIED)
        finally:
            s2.close()

    def test_pending_count(self):
        from app.db import session_scope
        from app.models import Account

        with session_scope() as s:
            s.query(Account).delete()
        self._seed(3, "p")
        q = AccountQueue("t")
        self.assertEqual(q.pending(), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
