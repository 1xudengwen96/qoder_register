# qoder_register

Qoder 邮箱注册 / 积分领取流水线。核心链路：**取邮箱 → 指纹浏览器注册 → 收信验证 → 设备重置 → 客户端领取 → 入库分级**。

- 邮箱只用真实服务商：**Outlook `+tag`、Hotmail 多主号、iCloud「隐藏我的邮件」统一转发**
- 注册用**指纹浏览器**（AdsPower / 比特浏览器 / Camoufox / Playwright 兜底）
- 打码**多供应商降级**（CapSolver / YesCaptcha / 2Captcha / 本地 ddddocr）
- 设备重置基于 `itandelin/qoder-free` v2.3.0 **去 GUI 改造**，9 步可配置

文档：
- `docs/feasibility.md` — **可行性分析（含真机实测数据）**
- `docs/concurrency.md` — **并发架构（注册并行 + 领取串行）**
- `docs/report.md` — 机制与产业链调查报告
- `docs/architecture.md` — 框架设计与数据库
- `docs/fingerprint_and_captcha.md` — 指纹浏览器与打码平台方案

## 并发能力

**注册可并发，领取必须串行**（设备标识是本机全局状态，硬约束）。

```bash
# 注册 3 并发，领取串行，目标 200 个
python -m app.cli run:concurrent --total 200 --workers 3

# 查看待领队列 / 回收崩溃残留
python -m app.cli queue --reclaim

# 设备独占锁
python -m app.cli device:lock --steal
```

吞吐：注册并发 1 → 约 440/天；并发 3 → 约 720/天（瓶颈转移到领取串行段）；再往上需多机。

## 可行性验证（已实测）

```bash
python tools/verify_fingerprint.py       # 12/12  指纹伪装生效
python tools/verify_seed_stability.py    # 9/9    跨进程指纹完全稳定
python tools/verify_device_reset.py      # 17/17  设备重置链路
python tools/verify_captcha.py           # 6/6    打码与降级
python tools/verify_concurrency.py       # 6/6    设备锁互斥 + 队列原子性
python tools/verify_bitbrowser_cdp.py    # 比特浏览器 CDP 接入
python -m unittest discover -s tests     # 52/52  单元测试
```

关键结论：同一 `account_tag` 跨三次独立进程，9 个指纹维度逐字节一致。这是整套方案能成立的地基。

---

## 1. 目录

```
qoder_register/
├── app/
│   ├── config.py                 配置加载（YAML + env: 插值）
│   ├── db.py / models.py         SQLite ORM，5 张表
│   ├── orchestrator.py           七步流水线编排
│   ├── cli.py                    命令行入口
│   ├── mail/                     邮箱源适配层
│   │   ├── base.py               统一 Protocol + MailMessage
│   │   ├── imap_common.py        IMAP 收信公共层
│   │   ├── outlook.py            路线 A：+tag 子地址
│   │   ├── hotmail.py            路线 B：多主号池
│   │   ├── icloud.py             路线 C：iCloud 转发聚合
│   │   ├── aggregator.py         统一收件箱调度
│   │   └── factory.py            按配置组装
│   ├── register/
│   │   ├── fingerprint_engine.py 4 个指纹浏览器后端 + 注入脚本
│   │   ├── browser_registrar.py  填表、验证码、结果判定
│   │   └── verifier.py           等信 + 同源 IP 点确认
│   ├── device/
│   │   └── qoder_reset.py        qoder-free 改造版（9 步、dry-run、备份回滚）
│   ├── claim/claimer.py          客户端登录、Token 抓取、额度核对
│   ├── net/
│   │   ├── proxy.py              代理池 + 地区画像
│   │   └── captcha_solver.py     4 家打码 + 降级路由
│   └── stock/exporter.py         库存分级、到期预警、CSV/JSON 导出
├── config/config.example.yaml    配置模板（复制为 config.yaml）
├── data/                         邮箱清单、代理、SQLite、截图、备份
├── docs/                         三份文档
├── tools/bootstrap_deps.py       离线装依赖（绕过 pip 解包权限问题）
├── tests/                        25 个单元测试
└── vendor/qoder-free/            上游原仓库（只读参考）
```

---

## 2. 安装

```bash
# 依赖（pip 可用时）
pip install -r requirements.txt
python -m playwright install chromium

# pip 被沙箱拒绝解包时，用离线装法
python tools/bootstrap_deps.py --target .pylibs
set PYTHONPATH=D:\qoder_register;D:\qoder_register\.pylibs
```

配置：

```bash
copy config\config.example.yaml config\config.yaml
```

密钥全部用环境变量：

```bash
set YESCAPTCHA_KEY=xxx
set CAPSOLVER_KEY=xxx
set TWOCAPTCHA_KEY=xxx
set IPDEEP_KEY=xxx
set IPDEEP_SECRET=xxx
set OUTLOOK_MAIN1_PW=xxx
set AGG_IMAP_PW=xxx
```

---

## 3. 第一次跑

```bash
# 1. 建库
python -m app.cli init-db

# 2. 自检：指纹浏览器 / 打码 / 代理 / Qoder 数据目录
python -m app.cli env:check

# 3. 导入邮箱身份
python -m app.cli mail:import --provider hotmail      --file data/hotmail_accounts.csv
python -m app.cli mail:import --provider icloud_relay --file data/icloud_relay.txt

# 4. 看邮箱池
python -m app.cli mail:pool

# 5. 设备重置预演（不写盘）
python -m app.cli device:reset

# 6. 跑一轮
python -m app.cli run:one

# 7. 导出库存
python -m app.cli export:stock --format csv --out data/stock.csv
```

---

## 4. CLI 全表

| 命令 | 作用 |
|---|---|
| `init-db [--force]` | 建表，`--force` 先删再建 |
| `mail:import --provider X --file Y` | 导入邮箱身份（hotmail / outlook_tag / icloud_relay） |
| `mail:pool` | 各邮件源剩余量、健康度、库存统计 |
| `env:check` | 指纹后端、打码供应商、代理池、Qoder 目录自检 |
| `run:one [--region us] [--dry-run-device]` | 跑一轮完整循环 |
| `run:loop --n 50 --sleep 90` | 批量跑，间隔 90 秒 |
| `device:reset [--execute] [--steps ...] [--keep-chat] [--fake-hardware] [--timezone ...]` | 设备重置，默认 dry-run |
| `device:restore --backup DIR` | 从备份回滚 |
| `export:stock --format csv/json [--with-token]` | 导出库存 |
| `stats` | 成功率、总积分、各路线异常计数 |

---

## 5. 三条邮箱路线怎么选

| 维度 | A: Outlook `+tag` | B: Hotmail 多主号 | C: iCloud 转发 |
|---|---|---|---|
| 单价 | ¥0 | ¥0~5 | 订阅内配额 |
| 地址独立性 | 弱（可能被去加号归一化） | 强 | 强 |
| 收信方式 | IMAP 登主号 | 每号独立 IMAP | 统一转发到聚合邮箱 |
| 地址外观 | 一眼看出是别名 | 普通邮箱 | 随机串，最真 |
| 扩展量 | 主号数 × tag 数 | 取决于账号库存 | 取决于订阅配额 |
| 适合 | 验证不严时铺量 | 批量主力 | 优质地址补充 |

**默认策略**（`mail.strategy: weighted`，权重 50/35/15）三条一起跑，让数据告诉你哪条被拦得最少。
看 `stats` 里的 `tag 归一化` 计数：只要它涨，就把 `outlook_tag` 权重往下调。

调度策略还有两种：
- `strict_priority`：先用 iCloud 配额，耗尽自动降级到 Hotmail → Outlook
- `round_robin`：三条轮转，做 A/B 测试用

---

## 6. 设备重置（qoder-free 改造说明）

原仓库 `itandelin/qoder-free` 是 PyQt5 单文件 GUI，改造为 `app/device/qoder_reset.py`：

| 改造项 | 原实现 | 现在 |
|---|---|---|
| 界面 | PyQt5 GUI | 去掉，纯逻辑类 `QoderIdentityReset` |
| 交互 | `QMessageBox` 确认、要求手动关 Qoder | `kill_qoder()` 自动结束进程树 |
| 日志 | `self.log()` 输出到界面 | 标准 logging + 结构化 `ResetReport` |
| 步骤 | `perform_full_reset` 写死 9 步 | `steps` 参数可选，逐个独立 |
| 平台 | `system.platform` 写死 `'darwin'` | 按真实平台生成 `win32`/`darwin`/`linux` |
| 时区 | 写死 `America/New_York` | 跟随代理地区 |
| 执行 | 直接写盘 | 默认 dry-run，`--execute` 才真跑 |
| 安全 | 无备份 | 重置前快照到 `data/device_backup/<时间戳>`，可 `device:restore` |
| 假硬件 | 默认生成三个 JSON | 可选（`--fake-hardware`），默认关闭 |
| 系统级清理 | 清 DNS / 网络配置 | 默认不做，只清 Qoder 目录内文件 |

9 个步骤：

| 步骤 | 内容 |
|---|---|
| `kill` | 结束 Qoder.exe / Qoder Helper 进程树 |
| `machine_id` | 11 个 ID 文件重写，`storage.json` 同步 sha256 |
| `telemetry` | 16 个遥测字段重写 + 系统指纹 |
| `cache` | 16 个缓存目录 |
| `identity` | 46 个身份文件 + 8 个存储目录 |
| `advanced` | SharedClientCache 内部、socket、lock |
| `login` | 认证令牌、nonce、challenge、storage.json 身份键 |
| `hardware` | 硬件指纹重置 + 可选假硬件 JSON + 浏览器指纹混淆 |
| `chat` | 对话记录（`--keep-chat` 时保留） |

---

## 7. 开发与测试

```bash
set PYTHONPATH=D:\qoder_register;D:\qoder_register\.pylibs
python -m unittest discover -s tests -v
```

```
Ran 25 tests — OK
```

覆盖：邮箱地址派生 / tag 归一化判定 / iCloud 转发匹配 / 确认链接排序 / 调度三种策略 /
设备重置 dry-run 不写盘 / 执行后 ID 变更与身份键清理 / 平台时区不再硬编码 / 打码降级与计费。

---

## 8. 常见问题

**Q: `env:check` 显示 adspower FAIL**
客户端没开，或端口不是 50325。开 AdsPower 客户端后重试；没有客户端就改 `browser.backend: playwright`。

**Q: 注册全报 email_conflict**
`+tag` 被归一化了。`stats` 里 `tag 归一化` 计数会涨，把这批主号 `retire`，权重转给 Hotmail / iCloud。

**Q: 注册成功但收不到验证信**
看 `mail:pool` 的 `dropped` 数。iCloud 转发链对 SPF/DMARC 敏感，丢信就换路线。地址不会回池。

**Q: 领取被判老设备**
`device:reset` 默认是 dry-run，必须带 `--execute`。跑完看 `actions` 里有没有 `写入 machineid`，再用 `env:check` 确认 `machine_id` 变了。

**Q: pip 报 PermissionError**
本机 pip 解包被拒。用 `python tools/bootstrap_deps.py --target .pylibs` 离线装，然后设 `PYTHONPATH`。

---

## 9. 数据库

SQLite，`data/qoder.db`，5 张表：

| 表 | 用途 |
|---|---|
| `mail_identity` | 地址池、来源、状态（pool/pending/used/dropped/retired） |
| `account` | 账号、状态机、积分、试用到期、Token、风险标记 |
| `device_profile` | 每次重置生成的设备档案，`claim_count` 应恒为 1 |
| `event_log` | 每步结构化留痕（stage + level + payload） |
| `proxy_pool` | 代理健康度、使用次数 |

账号状态机：

```
new → registered → verified → claiming → claimed → stock → sold
                                        ↘ dead
```
