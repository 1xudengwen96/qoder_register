"""并发架构验证：设备锁互斥、队列原子性、注册并行度。

不依赖真实注册（需要外部资源），用模拟生产者/消费者验证并发正确性。

用法：
    python tools/verify_concurrency.py
"""

from __future__ import annotations

import random
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.concurrent import AccountQueue, DeviceLock  # noqa: E402

LOCK = ROOT / "data" / ".verify_device.lock"


def test_lock_mutual_exclusion() -> tuple[bool, str]:
    """同一时刻只能有一个持有者。"""
    lock = DeviceLock(LOCK, stale_after=600)
    lock.release()

    held_max = 0
    active = 0
    peak_lock = threading.Lock()
    errors: list[str] = []

    def worker(i: int) -> None:
        nonlocal active, held_max
        lk = DeviceLock(LOCK, stale_after=600)
        for _ in range(30):
            if lk.acquire(timeout=2.0):
                with peak_lock:
                    active += 1
                    held_max = max(held_max, active)
                time.sleep(random.uniform(0.002, 0.008))
                with peak_lock:
                    active -= 1
                lk.release()
            else:
                time.sleep(0.01)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = held_max == 1 and not errors
    return ok, f"8 线程各抢 30 次，同时持有最大值 = {held_max}（必须为 1）"


def test_lock_stale_takeover() -> tuple[bool, str]:
    """僵尸锁能被接管。"""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text('{"pid": 999999, "ts": 1}', encoding="utf-8")
    lock = DeviceLock(LOCK, stale_after=1)
    ok = lock.acquire(timeout=0)
    lock.release()
    return ok, "写入时间戳为 1 的僵尸锁，能被立即接管"


def test_lock_blocking() -> tuple[bool, str]:
    """持锁期间别人拿不到；释放后能拿到。"""
    lock_a = DeviceLock(LOCK, stale_after=600)
    lock_b = DeviceLock(LOCK, stale_after=600)
    LOCK.unlink(missing_ok=True)

    got_a = lock_a.acquire(timeout=0)
    got_b = lock_b.acquire(timeout=0)
    lock_a.release()
    got_b2 = lock_b.acquire(timeout=0)
    lock_b.release()
    ok = got_a and not got_b and got_b2
    return ok, f"A 拿到={got_a}，B 被拒={not got_b}，A 释放后 B 拿到={got_b2}"


def test_queue_atomic() -> tuple[bool, str]:
    """多线程同时 claim_next，不能出现重复领取。"""
    from app.db import create_all, init_engine, session_scope
    from app.models import Account, AccountState

    init_engine("sqlite:///data/verify_concurrency.db")
    create_all(drop=True)

    total = 120
    with session_scope() as s:
        for i in range(total):
            s.add(Account(email=f"q{i}@test.local", password="p",
                          state=AccountState.VERIFIED))

    q = AccountQueue("verify")
    claimed: list[int] = []
    lock = threading.Lock()
    dupes: list[int] = []

    def consumer() -> None:
        while True:
            item = q.claim_next(max_wait=0)
            if item is None:
                return
            with lock:
                if item.account_id in claimed:
                    dupes.append(item.account_id)
                claimed.append(item.account_id)

    threads = [threading.Thread(target=consumer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = len(claimed) == total and len(set(claimed)) == total and not dupes
    return ok, (f"{total} 个账号，8 线程并发领取，领取 {len(claimed)} 个，"
                f"去重后 {len(set(claimed))} 个，重复 {len(dupes)} 个")


def test_queue_reclaim() -> tuple[bool, str]:
    """卡在 claiming 的账号能被回收。"""
    from app.db import init_engine, session_scope
    from app.models import Account, AccountState
    from datetime import datetime, timedelta

    init_engine("sqlite:///data/verify_concurrency.db")

    with session_scope() as s:
        a = Account(email="stuck@test.local", password="p",
                    state=AccountState.CLAIMING)
        s.add(a)
        s.flush()
        aid = a.id
        # 手动把 updated_at 改到 2 小时前
        a.updated_at = datetime.now() - timedelta(hours=2)

    q = AccountQueue("verify")
    n = q.reclaim_stale(older_than_minutes=30)

    from app.db import get_session
    s2 = get_session()
    try:
        ok = s2.get(Account, aid).state == AccountState.VERIFIED
    finally:
        s2.close()
    return bool(n >= 1 and ok), f"回收 {n} 个，账号状态已回到 verified = {ok}"


def test_register_parallelism() -> tuple[bool, str]:
    """模拟注册阶段的并行：N 个 worker 同时跑，耗时应接近单 worker。"""
    from concurrent.futures import ThreadPoolExecutor

    def fake_register(_i: int) -> int:
        time.sleep(1.0)          # 模拟一次注册
        return _i

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(fake_register, range(5)))
    parallel_elapsed = time.time() - t0

    t1 = time.time()
    for i in range(5):
        fake_register(i)
    serial_elapsed = time.time() - t1

    speedup = serial_elapsed / max(parallel_elapsed, 0.001)
    ok = speedup > 3.5 and len(results) == 5
    return ok, (f"5 个任务：串行 {serial_elapsed:.2f}s，5 并发 {parallel_elapsed:.2f}s，"
                f"加速 {speedup:.1f}x")


def test_timing_breakdown() -> tuple[bool, str]:
    """给出理论吞吐：注册并行后，瓶颈转移到领取串行段。"""
    reg_serial = 65      # 注册+验证单号秒数（实测区间 40~90）
    claim_serial = 40    # 设备重置+领取单号秒数（实测区间 20~60）
    lines = []
    for workers in (1, 3, 5, 8):
        # 注册产能
        reg_capacity = workers / reg_serial
        # 领取产能
        claim_capacity = 1 / claim_serial
        # 实际吞吐 = 两者较小值
        throughput = min(reg_capacity, claim_capacity)
        per_day = throughput * 3600 * 8
        bottleneck = "领取串行段" if claim_capacity < reg_capacity else "注册段"
        lines.append(f"  注册并发 {workers}: 日产量约 {per_day:.0f} 个，瓶颈在{bottleneck}")
    detail = "\n".join(lines)
    ok = True
    return ok, detail


def main() -> int:
    tests = [
        ("设备锁互斥", test_lock_mutual_exclusion),
        ("僵尸锁接管", test_lock_stale_takeover),
        ("锁阻塞与释放", test_lock_blocking),
        ("队列原子领取", test_queue_atomic),
        ("卡住账号回收", test_queue_reclaim),
        ("注册并行加速", test_register_parallelism),
    ]

    print("=== 并发架构验证 ===\n")
    passed = 0
    for name, fn in tests:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"异常：{type(exc).__name__}: {exc}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        for line in detail.splitlines():
            print(f"       {line}")
        passed += ok

    print("\n=== 吞吐测算 ===")
    _, detail = test_timing_breakdown()
    print(detail)

    print(f"\n{passed}/{len(tests)} 通过")
    LOCK.unlink(missing_ok=True)
    return 0 if passed == len(tests) else 2


if __name__ == "__main__":
    raise SystemExit(main())
