"""并发架构：注册并行 + 领取串行。

设计依据（见 docs/concurrency.md）：

- **注册阶段可并发**：每个账号一个独立指纹浏览器环境（独立 CDP 会话），
  彼此不共享 Cookie、缓存、localStorage，互不干扰。
- **领取阶段必须串行**：设备重置改的是"本机全局状态"（注册表、MAC、
  卷序列号、%APPDATA%\\Qoder），Qoder 客户端也同一时刻只允许一个实例。
  这是硬件约束，不是代码能绕过的。

模型：

    ┌── 注册 Worker × N（并行）──┐      ┌── 领取 Worker × 1（串行）──┐
邮箱池 → 取地址 → 指纹环境 → 注册 → 验证 → 入队 → 出队 → 设备重置 → 领取 → 入库

关键机制：
1. ``AccountQueue``：已验证账号的生产者-消费者队列（SQLite 持久化，可断点续跑）
2. ``DeviceLock``：设备独占锁（跨进程文件锁），防止两个领取 Worker 同时清设备
3. ``DeviceSlot``：一台机器一个槽位；多机部署时每台机器配不同 slot_id
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from app.config import get
from app.db import get_session, session_scope
from app.logging_setup import setup
from app.models import Account, AccountState, DeviceProfile, MailIdentity as MailRow, MailStatus

log = setup()


# ============================================================ 设备独占锁
class DeviceLock:
    """跨进程 + 跨线程设备独占锁。

    为什么需要跨进程：多机部署时多台机器各跑一个进程，若共享同一个 Qoder 目录，
    会互相覆盖。用文件锁保证原子性。

    为什么必须用 ``O_CREAT|O_EXCL``：
    "读文件 → 判断 → 写文件"不是原子操作，两个线程可能同时通过判断。
    必须用独占创建（原子）来抢锁，抢到再校验内容。

    僵尸锁处理：进程崩溃后锁文件残留，靠时间戳 + pid 存活检测自动接管。
    """

    def __init__(self, lock_path: str | Path = "data/.device.lock",
                 stale_after: int = 600) -> None:
        self.path = Path(lock_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stale_after = stale_after
        # 线程内的可重入锁：同一线程可重复 acquire
        self._local = threading.RLock()
        self._owned_by_this = 0

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if not pid:
            return False
        if os.name == "nt":
            try:
                import ctypes
                h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if not h:
                    return False
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _is_stale(self, info: dict) -> bool:
        ts = info.get("ts", 0)
        pid = int(info.get("pid", 0) or 0)
        if not ts:
            return True
        if time.time() - ts > self.stale_after:
            return True
        # 同机且进程已死 → 僵尸
        if pid and pid != os.getpid() and not self._pid_alive(pid):
            return True
        return False

    def _try_create(self, owner: str) -> bool:
        """原子创建锁文件。已存在则失败。"""
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            payload = json.dumps({
                "pid": os.getpid(), "ts": time.time(), "owner": owner,
            })
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def acquire(self, timeout: float = 0, poll: float = 0.1) -> bool:
        """抢锁。timeout=0 立即返回，>0 最长等待。"""
        with self._local:
            if self._owned_by_this > 0:
                self._owned_by_this += 1
                return True

        deadline = time.time() + timeout
        owner = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        while True:
            # 快速路径：原子创建
            if self._try_create(owner):
                with self._local:
                    self._owned_by_this = 1
                return True

            # 慢路径：检查是不是僵尸锁，尝试清理后重抢
            info = self._read()
            if self._is_stale(info):
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
                if self._try_create(owner):
                    with self._local:
                        self._owned_by_this = 1
                    return True

            if timeout == 0 or time.time() >= deadline:
                return False
            time.sleep(poll)

    def release(self) -> None:
        with self._local:
            if self._owned_by_this > 1:
                self._owned_by_this -= 1
                return
            self._owned_by_this = 0
        info = self._read()
        if info.get("pid") == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def refresh(self) -> None:
        """长任务续租，避免被判定为僵尸锁。"""
        with self._local:
            if self._owned_by_this <= 0:
                return
        info = self._read()
        if info.get("pid") == os.getpid():
            info["ts"] = time.time()
            try:
                self.path.write_text(json.dumps(info), encoding="utf-8")
            except Exception:
                pass

    def __enter__(self) -> "DeviceLock":
        if not self.acquire(timeout=0):
            raise RuntimeError("设备被其他进程占用，领取阶段不能并发")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# ============================================================ 账号队列
@dataclasses.dataclass(slots=True)
class QueueItem:
    account_id: int
    email: str
    password: str
    claimed_at: datetime | None = None
    applied: bool = False


class AccountQueue:
    """已验证账号的持久化队列（基于 SQLite 的 account 表，不额外建表）。

    生产者：注册 Worker 把 ``state=verified`` 的账号丢进来。
    消费者：领取 Worker 用 ``claim_next()`` 原子取一个，置为 ``claiming``。

    为什么用数据库而不是内存 queue：
    进程崩溃后重启能接着跑，不会丢已注册好的账号。
    """

    def __init__(self, slot_id: str = "local") -> None:
        self.slot_id = slot_id

    def put(self, account_id: int) -> None:
        """注册完成后调用：确保账号在 verified 状态待领。"""
        with session_scope() as s:
            a = s.get(Account, account_id)
            if a and a.state in (AccountState.REGISTERED, AccountState.VERIFIED):
                a.state = AccountState.VERIFIED

    def claim_next(self, max_wait: float = 0) -> QueueItem | None:
        """原子取一个 verified 账号，置为 claiming。

        为什么不能用"先 SELECT 再 UPDATE"：
        SQLite 默认隔离级别下，两个线程可能同时 SELECT 到同一行，
        然后各自 UPDATE，造成重复领取（实测 120 个号被领了 134 次）。

        正确做法：用 ``UPDATE ... WHERE id=(SELECT ... LIMIT 1) AND state='verified'``
        并在同一事务里检查 ``rowcount``。rowcount=1 才算抢到，
        rowcount=0 说明被别人抢先，重试下一行。
        """
        from sqlalchemy import select, update

        deadline = time.time() + max_wait
        while True:
            got_id: int | None = None
            with session_scope() as s:
                # 取候选 id（不带锁读，仅作参考）
                sub = (select(Account.id)
                       .where(Account.state == AccountState.VERIFIED)
                       .order_by(Account.id.asc())
                       .limit(1))
                cand = s.execute(sub).scalar_one_or_none()
                if cand is not None:
                    # 带条件更新：只有仍是 verified 才改成功
                    stmt = (update(Account)
                            .where(Account.id == cand,
                                   Account.state == AccountState.VERIFIED)
                            .values(state=AccountState.CLAIMING,
                                    updated_at=datetime.now()))
                    res = s.execute(stmt)
                    if res.rowcount == 1:
                        got_id = cand

            if got_id is not None:
                s2 = get_session()
                try:
                    a = s2.get(Account, got_id)
                    return QueueItem(got_id, a.email, a.password or "")
                finally:
                    s2.close()

            if max_wait <= 0 or time.time() >= deadline:
                return None
            time.sleep(0.5)

    def pending(self) -> int:
        with session_scope() as s:
            return s.query(Account).filter(
                Account.state.in_([AccountState.REGISTERED, AccountState.VERIFIED])
            ).count()

    def reclaim_stale(self, older_than_minutes: int = 30) -> int:
        """把卡在 claiming 太久的账号退回 verified，防止进程崩溃后永久阻塞。"""
        cutoff = datetime.now() - timedelta(minutes=older_than_minutes)
        n = 0
        with session_scope() as s:
            rows = (s.query(Account)
                    .filter(Account.state == AccountState.CLAIMING)
                    .filter(Account.updated_at < cutoff)
                    .all())
            for a in rows:
                a.state = AccountState.VERIFIED
                n += 1
        return n


# ============================================================ 并发流水线
@dataclasses.dataclass(slots=True)
class WorkerStats:
    registered: int = 0
    register_failed: int = 0
    claimed: int = 0
    claim_failed: int = 0
    credits: int = 0


class ConcurrentPipeline:
    """注册并行 + 领取串行的编排器。

    用法：

        pipe = ConcurrentPipeline(cfg, register_workers=4)
        pipe.run(total=100)

    内部结构：

    - ``ThreadPoolExecutor(register_workers)``：跑注册（每个 worker 一个指纹环境）
    - 主线程：串行跑领取（设备锁 + 客户端单实例）
    """

    def __init__(
        self,
        cfg,
        register_workers: int = 3,
        slot_id: str = "local",
        lock_path: str = "data/.device.lock",
        device_wait: float = 1800,
    ) -> None:
        from app.orchestrator import Pipeline

        self.cfg = cfg
        self.pipe = Pipeline(cfg)
        self.register_workers = max(1, register_workers)
        self.slot_id = slot_id
        self.queue = AccountQueue(slot_id)
        self.device_lock = DeviceLock(lock_path)
        self.device_wait = device_wait
        self.stats = WorkerStats()
        self._stop = threading.Event()
        self._stats_lock = threading.Lock()

    # ---------------------------------------------------------- 注册 Worker
    def _register_worker(self, index: int, region: str | None) -> list[tuple[int, str]]:
        """一个注册 worker：取邮箱 → 注册 → 验证 → 入队。返回 (account_id, 结果)。"""
        results: list[tuple[int, str]] = []
        region = region or get(self.cfg, "region", "us")
        while not self._stop.is_set():
            try:
                rep = self.pipe.register_only(region=region, worker=index)
            except Exception as exc:
                log.exception("注册 worker %d 异常", index)
                results.append((-1, f"exception: {exc}"))
                break

            if rep.account_id is None and rep.stage == "mail":
                log.info("注册 worker %d：邮箱池已空，退出", index)
                break

            if rep.ok:
                self.queue.put(rep.account_id)
                with self._stats_lock:
                    self.stats.registered += 1
                log.info("worker %d 注册成功 id=%s %s",
                         index, rep.account_id, rep.email)
                results.append((rep.account_id, "ok"))
            else:
                with self._stats_lock:
                    self.stats.register_failed += 1
                log.warning("worker %d 注册失败 %s: %s",
                            index, rep.stage, rep.message)
                results.append((rep.account_id or -1, f"{rep.stage}:{rep.message}"))
            return results
        return results

    # ---------------------------------------------------------- 领取（串行）
    def _claim_one(self) -> bool:
        """取一个账号，串行领取。返回是否成功。"""
        if not self.device_lock.acquire(timeout=self.device_wait):
            log.error("设备锁等待超时，本轮跳过领取")
            return False
        try:
            item = self.queue.claim_next(max_wait=60)
            if item is None:
                return False
            # 长任务续租
            self.device_lock.refresh()
            rep = self.pipe.claim_only(item.account_id, self.device_lock)
            with self._stats_lock:
                if rep.ok:
                    self.stats.claimed += 1
                    self.stats.credits += rep.credits
                else:
                    self.stats.claim_failed += 1
            return rep.ok
        finally:
            self.device_lock.release()

    # ---------------------------------------------------------- 主循环
    def run(self, total: int, region: str | None = None) -> WorkerStats:
        """跑 total 个账号。注册并行，领取串行。"""
        log.info("并发流水线启动：注册并发 %d，领取串行，目标 %d 个",
                 self.register_workers, total)
        self.queue.reclaim_stale()

        produced = 0
        consumed = 0

        with ThreadPoolExecutor(max_workers=self.register_workers,
                                thread_name_prefix="reg") as pool:
            futures = []
            for i in range(self.register_workers):
                futures.append(pool.submit(self._register_worker, i, region))

            # 边注册边领取
            while consumed < total:
                # 先尝试领取（串行，主线程）
                if self._claim_one():
                    consumed += 1
                    log.info("已领取 %d/%d", consumed, total)
                else:
                    # 队列空，等生产
                    done = [f for f in futures if f.done()]
                    for f in done:
                        futures.remove(f)
                    if not futures and self.queue.pending() == 0:
                        log.info("注册全部结束且队列为空，停止")
                        break
                    time.sleep(2)

            self._stop.set()
            for f in as_completed(futures, timeout=600):
                pass

        log.info("完成：注册成功 %d，注册失败 %d，领取成功 %d，领取失败 %d，总积分 %d",
                 self.stats.registered, self.stats.register_failed,
                 self.stats.claimed, self.stats.claim_failed, self.stats.credits)
        return self.stats

    def report(self) -> str:
        s = self.stats
        return (f"注册 {s.registered}（失败 {s.register_failed}） / "
                f"领取 {s.claimed}（失败 {s.claim_failed}） / "
                f"积分 {s.credits} / 队列待领 {self.queue.pending()}")
