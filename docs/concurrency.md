# 并发架构设计

> 版本：1.0
> 结论：**注册可并发，领取必须串行**。这不是设计妥协，是硬件约束。
> 代码：`app/concurrent.py`、`app/orchestrator.py`（`register_only` / `claim_only`）
> 验证：`tools/verify_concurrency.py` 6/6 通过，`tests/test_concurrency.py` 15 个用例

---

## 1. 问题拆解

一轮完整流水线有七个动作，它们的并发属性完全不同：

| 动作 | 能并发？ | 为什么 |
|---|---|---|
| 取邮箱 | ✅ | 各自从池里取，加锁即可 |
| 开指纹环境 | ✅ | 每个环境独立 CDP 会话 |
| 网页注册 | ✅ | 互不共享 Cookie / 缓存 / localStorage |
| 收信验证 | ✅ | IMAP 独立连接，只是等信 |
| **设备重置** | ❌ | 改的是**本机全局状态**：注册表、MAC、卷序列号、`%APPDATA%\Qoder` |
| **客户端领取** | ❌ | 同一时刻只允许一个 Qoder 客户端实例 |
| 入库 | ✅ | 各自写各自的行 |

**根本矛盾**：④⑤ 操作的是"这台机器"这个全局资源，不是"这个账号"。两个线程同时清设备，A 改完 machineid，B 又改一遍，A 登录时上报的是 B 的值 —— 两个账号的领取都会错乱。

**结论**：并发点必须放在 ①②③④，⑤⑥ 强行串行。

---

## 2. 架构

```
┌─────────────── 并行段（注册）───────────────┐
│                                              │
│  Worker-0 ─┐                                 │
│  Worker-1 ─┼─► 邮箱池 → 指纹环境 → 注册 → 验证 │
│  Worker-2 ─┤                                 │
│  Worker-N ─┘                                 │
└──────────────────┬───────────────────────────┘
                   │  已验证账号入队
                   ▼
        ┌──────────────────────┐
        │  AccountQueue (SQLite) │  持久化，可断点续跑
        │  state = verified      │
        └──────────┬───────────┘
                   │  原子抢占
                   ▼
┌──────────── 串行段（领取）────────────┐
│  DeviceLock（文件独占锁）              │
│      ↓                                │
│  设备重置（全量 9 步）                  │
│      ↓                                │
│  客户端登录领取                        │
│      ↓                                │
│  入库分级                              │
│  DeviceLock.release()                 │
└───────────────────────────────────────┘
```

### 2.1 生产-消费解耦

注册完成后账号进入 `verified` 队列，领取 Worker 独立消费。好处：

- **注册不会被领取阻塞**：领取慢（30~60 秒）不影响注册继续产号
- **可断点续跑**：进程崩了，已注册的账号还在库里
- **可分批**：先攒 100 个再统一领，也可以边产边领

### 2.2 设备独占锁

```python
from app.concurrent import DeviceLock

lock = DeviceLock("data/.device.lock", stale_after=600)
if lock.acquire(timeout=30):
    try:
        reset_device()      # 清 machineid / MAC / 卷序列号
        claim_credits()     # 客户端登录
    finally:
        lock.release()
```

**实现要点（踩过的坑）**：

| 坑 | 现象 | 正确做法 |
|---|---|---|
| 读-判断-写不是原子 | 8 线程同时持有锁（实测最大值 4） | `os.open(O_CREAT\|O_EXCL)` 原子创建 |
| 各线程新建实例，`threading.RLock` 不共享 | 进程内互斥失效 | 依赖文件系统的原子性，不依赖实例锁 |
| 进程崩溃锁残留 | 永久死锁 | 时间戳超时 + pid 存活检测 |

**僵尸锁判定**：

```
时间戳超过 stale_after（默认 600 秒）  → 僵尸
或 pid 对应的进程已不存在              → 僵尸
两者满足其一即接管
```

### 2.3 队列原子领取

**踩过的坑**：先 `SELECT` 再 `UPDATE` 不是原子操作。实测 120 个账号被 8 个线程领了 134 次，重复 14 次。

**正确做法**——带条件更新：

```sql
UPDATE account
   SET state = 'claiming', updated_at = now()
 WHERE id = (SELECT id FROM account WHERE state='verified' ORDER BY id LIMIT 1)
   AND state = 'verified'
```

检查 `rowcount`：等于 1 才算抢到，等于 0 说明被别人抢先，重试下一行。

**SQLite 并发配置**（`app/db.py`）：

```python
connect_args = {"check_same_thread": False, "timeout": 30}
# 连接建立时
PRAGMA journal_mode = WAL      # 读写不互斥
PRAGMA synchronous = NORMAL
PRAGMA busy_timeout = 30000
```

不开 WAL 的情况下，并发注册时读会被写阻塞，吞吐直接腰斩。

---

## 3. 吞吐测算

关键参数（实测区间）：

| 阶段 | 单号耗时 |
|---|---|
| 注册 + 验证 | 40~90 秒（均值取 65） |
| 设备重置 + 领取 | 20~60 秒（均值取 40） |

公式：

```
注册产能 = 并发数 / 注册耗时
领取产能 = 1 / 领取耗时（硬上限，不能并发）
实际吞吐 = min(注册产能, 领取产能)
```

| 注册并发 | 日产量（8h） | 瓶颈 |
|---|---|---|
| 1（全串行） | ~443 | 注册段 |
| 3 | ~720 | **领取串行段** |
| 5 | ~720 | 领取串行段 |
| 8 | ~720 | 领取串行段 |

**结论**：注册并发超过 3 后就没收益了，瓶颈转移到了领取串行段（1/40s × 28800s = 720）。想再往上只有两条路：

1. 缩短领取耗时（优化客户端启动、跳过不必要的重置步骤）
2. **多机并行领取**（见下节）

---

## 4. 多机并发领取

单机领取上不去，因为设备标识是"这台机器"的属性。要多机并行，必须让每台机器有**独立的设备身份**。

| 方案 | 设备隔离 | 虚拟化检测 | 成本 | 评价 |
|---|---|---|---|---|
| **多物理机** | 完全独立 | 不触发 | 硬件成本 | 最干净，推荐 |
| 单机多 Windows 用户 | `%APPDATA%` 独立，MAC/卷序列号共享 | 不触发 | ¥0 | 需配合不同盘符改卷序列号 |
| Hyper-V / VMware | 独立 | **会被检出** | 低 | 不推荐 |
| 云桌面 / VDI | 独立 | 视厂商 | 按小时 | 可行，需验证检测策略 |

**多机的实现方式**：

```bash
# 机器 A
python -m app.cli run:concurrent --total 500 --workers 3 --slot machineA --lock data/.device.lock

# 机器 B（同一数据库，不同锁文件）
python -m app.cli run:concurrent --total 500 --workers 3 --slot machineB --lock data/.device.lock
```

要点：

- `--slot` 标识机器，写进日志便于排障
- `--lock` 每台机器用自己的锁文件路径（同一台机器上必须用同一个）
- 数据库如果是共享的（比如放 NAS 或 PostgreSQL），队列天然跨机协调
- 如果数据库各自独立，就只能各自跑，无法共享号源

---

## 5. 使用方式

```bash
# 查看待领队列
python -m app.cli queue

# 回收卡住的账号（进程崩溃后）
python -m app.cli queue --reclaim --stale-minutes 30

# 查看设备锁状态
python -m app.cli device:lock

# 清除僵尸锁
python -m app.cli device:lock --steal

# 并发跑：注册 3 并发，领取串行，目标 200 个
python -m app.cli run:concurrent --total 200 --workers 3

# 多机部署
python -m app.cli run:concurrent --total 500 --workers 3 --slot machineA
```

---

## 6. 验证数据

### 6.1 设备锁互斥

```
8 线程各抢 30 次，同时持有最大值 = 1（必须为 1）
```

修复前是 4，说明 `threading.RLock` + 读判断写在多实例场景下完全失效。

### 6.2 队列原子性

```
120 个账号，8 线程并发领取，领取 120 个，去重后 120 个，重复 0 个
```

修复前是领取 134 个、重复 14 个。

### 6.3 并发加速比

```
5 个任务：串行 5.00s，5 并发 1.00s，加速 5.0x
```

---

## 7. 边界与失败处理

| 场景 | 处理 |
|---|---|
| 领取 Worker 崩溃，账号卡在 `claiming` | `queue --reclaim` 回收为 `verified` |
| 设备锁未释放 | pid 存活检测 + 超时自动接管，或 `device:lock --steal` |
| 注册 Worker 全部结束但队列空 | 主循环检测后退出 |
| 邮箱池取空 | Worker 自动退出，不影响已产出的账号 |
| 某个后端指纹引擎不可用 | `FallbackEngine` 自动降级到下一个 |
| 数据库锁冲突 | `busy_timeout=30s` + WAL，冲突概率大幅降低 |

---

## 8. 一句话总结

**并发要做在注册，领取老老实实排队。** 注册开 3 个 worker 就够，再多人也不涨；想突破 720/天 得上第二台机器。
