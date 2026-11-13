# qoder_register 框架设计文档

> 版本：1.0
> 对应报告：`docs/report.md` v2.0
> 架构选型依据：原报告第 4 章（批量注册）、第 5 章（设备重置）、第 4 章邮箱路线（A/B/C）

---

## 1. 设计目标与约束

| 目标 | 说明 |
|---|---|
| 邮箱身份只用 Outlook / Hotmail / iCloud | 不引入自建域名、catch-all、临时邮箱服务 |
| 三种邮箱源统一收口 | 无论信从哪个入口来，下游只读一个"收件箱" |
| 单机可跑 | 一台物理机跑完整循环，不依赖集群 |
| 状态可恢复 | 每一步落库，进程中断后可续跑 |
| 模块可替换 | 邮箱源、浏览器引擎、打码服务、设备重置器都是插件 |
| 可审计 | 每个账号有完整状态流水，便于对账与复盘 |

约束：

- 领取阶段必须跑在**物理机**上（虚拟机检测不触发是前提）。
- 设备重置与客户端登录**不能并发**，同一时刻只允许一个客户端实例。
- 邮箱收信与注册共享同一账号上下文（`account_id`），避免取错信。

---

## 2. 总体架构

```
┌───────────────────────────────────────────────────────────────────────────┐
│                              cli.py (Typer)                               │
│    init-db | mail:pool | run:one | run:loop | device:reset | export:stock │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────────────────┐
│                         orchestrator.Pipeline                             │
│  pick_mail → alloc_identity → register → wait_verify → activate           │
│      → device_reset → claim → harvest → to_stock → loop                    │
└───┬──────────┬──────────┬──────────┬──────────┬──────────┬───────────────┘
    │          │          │          │          │          │
┌───▼───┐ ┌────▼────┐ ┌───▼─────┐ ┌──▼──────┐ ┌▼────────┐ ┌▼─────────────┐
│ mail  │ │ register│ │ verify  │ │ device  │ │ claim   │ │ stock/export │
│ 模块  │ │ 模块    │ │ 模块    │ │ reset   │ │ 模块    │ │ 模块          │
└───┬───┘ └────┬────┘ └───┬─────┘ └──┬──────┘ └┬────────┘ └┬─────────────┘
    │          │          │          │         │           │
┌───▼──────────▼──────────▼──────────▼─────────▼───────────▼───────────────┐
│                            adapters (插件层)                              │
│  outlook_imap | hotmail_imap | icloud_relay | playwright_browser          │
│  captcha_client | device_reset | qoder_client_api                         │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────────────────┐
│                      storage (SQLite → PostgreSQL)                        │
│  mail_identity | account | event_log | device_profile | proxy_pool        │
└───────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 模块职责

| 模块 | 文件 | 职责 | 不做什么 |
|---|---|---|---|
| config | `app/config.py` | 读 YAML + 环境变量，提供强类型配置 | 不含业务逻辑 |
| storage | `app/db.py`, `app/models.py` | 建表、会话、状态机字段 | 不判断业务规则 |
| 邮箱源 | `app/mail/*` | 取地址、收信、抽链接，统一 `MailProvider` 接口 | 不注册、不领取 |
| register | `app/register/registrar.py` | 驱动浏览器填表、过验证码、提交 | 不管设备、不管领取 |
| verify | `app/register/verifier.py` | 轮询/订阅收件箱，命中确认链接并访问 | 不判账号业务状态 |
| device | `app/device/reset.py` | 关客户端、清标识、改 MAC/卷序列号、清 SafeStore | 不登录 |
| claim | `app/claim/claimer.py` | 启动客户端登录、确认额度到账、抓取 Token | 不改设备 |
| stock | `app/stock/exporter.py` | 库存分级、到期预警、导出 CSV/JSON | 不跑循环 |
| proxy | `app/net/proxy.py` | 代理池分配、健康检查、黑名单 | 不碰账号数据 |
| captcha | `app/net/captcha.py` | 打码接口封装、重试、计费统计 | — |
| orchestrator | `app/orchestrator.py` | 串起全流程、事务边界、失败重试与断点续跑 | 不实现具体技术动作 |

---

## 4. 邮箱源适配层（本方案核心）
### 4.1 统一抽象

```python
class MailProvider(Protocol):
    name: str

    def acquire(self, n: int) -> list[MailIdentity]:
        """从池里拿 n 个可用地址（未注册过 Qoder）"""

    def wait_for_mail(self, ident: MailIdentity, timeout: int,
                      match: Callable[[MailMessage], bool]) -> MailMessage | None:
        """等待并返回匹配的邮件"""

    def extract_links(self, msg: MailMessage) -> list[str]:
        """从 HTML/纯文本里抽链接"""

    def health(self) -> ProviderHealth:
        """用于 UI/CLI 展示：配额、剩余地址、最近错误"""
```

下游 `registrar` 与 `verifier` 只依赖这个接口。切换路线不改业务代码，只改配置。

### 4.2 三条实现

| 实现 | 类 | 地址来源 | 收信方式 | 关键参数 |
|---|---|---|---|---|
| A: Outlook `+tag` | `OutlookTagProvider` | 主号池 + tag 生成器 | IMAP 登录主号 | `main_accounts[]`, `tag_prefix`, `max_tags_per_account` |
| B: Hotmail 多主号 | `HotmailProvider` | 独立主号清单（CSV/DB 导入） | 每号独立 IMAP | `credentials_file`, `pool_size` |
| C: iCloud 隐藏地址 | `ICloudRelayProvider` | 手动/半自动在 iCloud 后台生成，导入清单 | 统一转发到 `aggregator_mailbox` 后 IMAP 读它 | `relay_addresses_file`, `aggregator_imap` |

### 4.3 路线 C 的转发链设计（重点）

```
iCloud 隐藏地址 n 个  ──┐
Outlook 主号 m 个 ─────┼──► 统一聚合邮箱 Aggregator (IMAP)
Hotmail 主号 k 个 ─────┘        │
                                ▼
                     MailAggregator.poll()
                       按收件人 To/To-Delivered-To 头匹配地址 → account_id
                                ▼
                      写入 mail_identity.last_message
```

设计要点：

1. **地址匹配靠邮件头**。iCloud 转发后 `To:` 头保留原始隐藏地址，聚合器以 `Delivered-To` / `To` 双字段匹配，匹配不上时回退到"最近 60 秒内 pending 账号"兜底并打标 `uncertain`。
2. **iCloud 地址清单是离线生成的**。iCloud 后台批量生成隐藏地址没有稳定公开 API，框架把它做成"导入清单 + 手动补录"的形态：`data/icloud_relay.txt` 一行一个地址，导入时校验与 Apple ID 主邮箱的转发关系。
3. **配额感知**。`ICloudRelayProvider.health()` 读本地计数与配置上限，剩余 < 20% 时 CLI 告警。
4. **丢弃信检测**。转发链对 SPF/DMARC 敏感，框架记录"注册后 10 分钟未收到任何信"的账号，标 `mail_dropped`，不做重试，直接释放地址回池（换路线补）。

### 4.4 混合调度策略

```yaml
mail:
  strategy: "weighted"       # weighted | strict_priority | round_robin
  weights: { outlook_tag: 50, hotmail: 35, icloud_relay: 15 }
  strict_priority_order: [icloud_relay, hotmail, outlook_tag]
```

- `weighted`：默认，按比例取，兼顾地址"真实度"与量。
- `strict_priority`：先用优质地址（iCloud），配额耗尽自动降级。
- `round_robin`：三条路线轮转，用于观测哪条被拦得最少（A/B 测试）。

---

## 5. 数据库设计

SQLite 起步，字段类型兼容 PostgreSQL（迁移只需换 URL）。

### 5.1 `mail_identity`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| provider | TEXT | `outlook_tag` / `hotmail` / `icloud_relay` |
| address | TEXT UNIQUE | 实际用于注册的地址 |
| parent_account | TEXT | 主号（A/B）或 Apple ID 主邮箱（C） |
| status | TEXT | `pool` / `pending` / `used` / `dropped` / `retired` |
| qoder_account_id | INTEGER FK | 关联账号 |
| created_at / used_at | DATETIME | |
| note | TEXT | |

索引：`(provider, status)`、`address`。

### 5.2 `account`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| email | TEXT UNIQUE | 注册邮箱 |
| password | TEXT | 独立强密码 |
| username | TEXT | 用户名 |
| state | TEXT | `new`→`registered`→`verified`→`claiming`→`claimed`→`stock`→`sold` / `dead` |
| credits | INTEGER | 到账额度 |
| plan | TEXT | `pro_trial` 等 |
| trial_expire_at | DATETIME | 试用到期 |
| register_ip | TEXT | |
| device_profile_id | INTEGER FK | 领取时使用的设备档案 |
| token_json | TEXT | Cookie / OAuth Token 快照 |
| risk_flags | JSON | `mail_dropped` / `tag_normalized` / `proxy_dirty` 等 |
| created_at / claimed_at / updated_at | DATETIME | |

索引：`state`、`trial_expire_at`、`(state, trial_expire_at)`。

### 5.3 `device_profile`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| machine_id | TEXT | 重置后生成的 |
| telemetry_id | TEXT | |
| mac_address | TEXT | 改写的 |
| volume_serial | TEXT | |
| reset_at | DATETIME | |
| claim_count | INTEGER | 该档案已用次数（应为 1） |
| account_id | INTEGER FK | 绑定的账号 |

### 5.4 `event_log`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| account_id / mail_identity_id | INTEGER | |
| stage | TEXT | `register` / `verify` / `reset` / `claim` / `harvest` |
| level | TEXT | `info` / `warn` / `error` |
| message | TEXT | |
| payload | JSON | 截图路径、HTTP 摘要、打码结果 |
| created_at | DATETIME | |

### 5.5 `proxy_pool`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| endpoint | TEXT UNIQUE | `http://user:pass@host:port` |
| region | TEXT | |
| health | TEXT | `ok` / `slow` / `banned` |
| last_used_at | DATETIME | |
| use_count / fail_count | INTEGER | |

---

## 6. 状态机

```
mail_identity:  pool → pending → used
                     ↘ dropped(收信超时)  ↘ retired(主号弃用)

account:
   new ──register成功──► registered
    │                        │
    │                        ├─verify成功──► verified
    │                        └─超时/失败──► dead
    │
    verified ──device_reset──► claiming ──claim成功──► claimed ──► stock ──► sold
                                      └─claim失败──► dead（回滚 device_profile）
```

规则：

- **一次领取只允许一个 `device_profile`**，且 `claim_count` 不得超过 1，超了直接判异常并落 `risk_flags`。
- `dead` 状态的账号保留邮箱记录，防止地址回池被重复使用。
- `mail_dropped` 不重试，避免拖长单号周期。

---

## 7. 流程编排（Pipeline）

```python
# 伪代码，真实实现见 app/orchestrator.py
async def run_one(cfg, db):
    ident = mail.pick()                       # ① 取邮箱
    acct  = account.create(ident)             # 建账号，state=new
    proxy = proxy_pool.acquire(region=cfg.region)
    env   = browser.launch(profile=random_profile(), proxy=proxy)
    ok    = registrar.register(env, ident, acct)      # ③ 注册
    if not ok: return mark_dead(acct, "register_failed")

    msg   = verifier.wait_link(ident, timeout=600)    # ④ 收信
    if not msg:  return mark_dead(acct, "mail_dropped")

    verifier.visit(msg.link, proxy=proxy)             # 同源 IP
    acct.state = VERIFIED

    prof  = device_reset.perform()                    # ② 清设备
    prof.bind(acct)

    claim = claimer.perform(acct, prof)               # ⑤ 登录领取
    if claim.ok:
        acct.credits = claim.credits
        acct.trial_expire_at = claim.expire_at
        acct.token_json = claim.token
        acct.state = CLAIMED
    else:
        prof.release(); acct.state = DEAD

    stock.put(acct)                                    # ⑥ 入库
    return acct
```

失败重试边界：

| 阶段 | 重试次数 | 重试前动作 |
|---|---|---|
| register | 1 次 | 换指纹环境 + 换代理 |
| verify | 0 次（邮件超时不重试） | — |
| device_reset | 2 次 | 重跑重置，校验 Machine ID 已变 |
| claim | 1 次 | 重清设备再登录 |

---

## 8. 配置示例

```yaml
# config/config.yaml
region: US
concurrency: 1                 # 领取阶段强制串行

mail:
  strategy: weighted
  weights: { outlook_tag: 50, hotmail: 35, icloud_relay: 15 }
  outlook_tag:
    main_accounts:
      - { user: "main1@outlook.com", password: "env:OUTLOOK_MAIN1_PW", imap: "outlook.office365.com" }
    tag_prefix: "q"
    max_tags_per_account: 8
  hotmail:
    credentials_file: "data/hotmail_accounts.csv"   # email,password,imap_host
  icloud_relay:
    relay_addresses_file: "data/icloud_relay.txt"
    aggregator_imap: { user: "agg@outlook.com", password: "env:AGG_PW", host: "outlook.office365.com" }
    quota_total: 100

browser:
  engine: playwright
  channel: chromium
  fingerprint_profile: camoufox
  headless: false

captcha:
  provider: yescaptcha
  api_key: "env:YESCAPTCHA_KEY"

device:
  target_app: qoder
  kill_processes: ["Qoder.exe"]
  reset_items: [machine_id, telemetry_id, hardware_cache, mac, volume_serial, safestore]

claim:
  client_path: "C:\\Program Files\\Qoder\\Qoder.exe"
  wait_credit_timeout: 120
  capture_token: true

proxy:
  source: file
  file: "data/proxies.txt"
  region_filter: true

storage:
  url: "sqlite:///data/qoder.db"
```

---

## 9. 目录结构

```
D:\qoder_register\
├── README.md
├── requirements.txt
├── config/
│   └── config.example.yaml
├── data/                    # 运行期数据（地址清单、代理、截图、SQLite）
├── docs/
│   ├── report.md
│   └── architecture.md
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── db.py
│   ├── models.py
│   ├── logging_setup.py
│   ├── orchestrator.py
│   ├── cli.py
│   ├── mail/
│   │   ├── __init__.py
│   │   ├── base.py          # MailProvider 协议 + MailIdentity/MailMessage
│   │   ├── outlook.py       # OutlookTagProvider (IMAP + tag)
│   │   ├── hotmail.py       # HotmailProvider (多主号)
│   │   ├── icloud.py        # ICloudRelayProvider (转发聚合)
│   │   ├── aggregator.py    # 统一收件箱轮询与匹配
│   │   └── factory.py
│   ├── register/
│   │   ├── __init__.py
│   │   ├── registrar.py
│   │   └── verifier.py
│   ├── device/
│   │   ├── __init__.py
│   │   └── reset.py
│   ├── claim/
│   │   ├── __init__.py
│   │   └── claimer.py
│   ├── net/
│   │   ├── __init__.py
│   │   ├── proxy.py
│   │   └── captcha.py
│   └── stock/
│       ├── __init__.py
│       └── exporter.py
└── tests/
    └── test_mail_matching.py
```

---

## 10. CLI 设计

| 命令 | 作用 |
|---|---|
| `python -m app.cli init-db` | 建库建表 |
| `python -m app.cli mail:import --provider hotmail --file data/hotmail_accounts.csv` | 导入邮箱身份 |
| `python -m app.cli mail:pool` | 查看各路线剩余地址、健康状态 |
| `python -m app.cli run:one` | 跑一轮完整循环 |
| `python -m app.cli run:loop --n 50 --sleep 90` | 批量跑，间隔秒数 |
| `python -m app.cli device:reset --dry-run` | 预演设备重置（只列动作不执行） |
| `python -m app.cli claim:retry --account-id 12` | 单账号重试领取 |
| `python -m app.cli export:stock --format csv --out data/stock.csv` | 导出库存 |
| `python -m app.cli stats` | 成功率、单号成本、各路线拦截率 |

---

## 11. 可观测性

| 指标 | 采集点 | 用途 |
|---|---|---|
| register_success_rate | registrar | 判断指纹/代理是否失效 |
| mail_drop_rate(by provider) | verifier | 判断哪条邮箱路线被丢信 |
| tag_normalize_hits | registrar | 判断 `+tag` 是否被归一化，触发切换 |
| reset_verify_fail | device | 判断工具是否过时 |
| claim_success_rate | claimer | 整体健康度 |
| cost_per_account | 汇总 | 对账 |

告警阈值写进 `config.yaml`，超阈值在 `stats` 里红标。

---

## 12. 关键风险与对应设计

| 风险 | 框架层的应对 |
|---|---|
| `+tag` 被归一化 | `risk_flags.tag_normalized` 计数超阈值自动降权 `outlook_tag`，调度器转向 B/C |
| iCloud 配额耗尽 | `health()` 报剩余，`strict_priority` 自动降级 |
| 收信丢信 | 10 分钟无信判 `mail_dropped`，地址退役不重试 |
| 重置工具失效 | `device.reset` 做"重置后校验 Machine ID 是否变化"，失败两次告警 |
| 代理成批被封 | `proxy_pool` 健康检查 + 自动摘除 |
| 客户端与重置并发 | `concurrency: 1` + 设备档案互斥锁 |
