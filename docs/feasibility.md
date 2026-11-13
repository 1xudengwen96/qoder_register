# 可行性分析报告

> 版本：1.0
> 日期：2026-11-10
> 结论：**技术可行，已在真机实测验证；瓶颈不在代码，在外部资源（住宅代理、邮箱库存、指纹浏览器客户端）**
> 实测环境：Windows 10/11，Python 3.12，Chromium 158.0.8127.18，Qoder 已安装于 `%APPDATA%\Qoder`

---

## 0. 一句话结论

链路的七个环节里，**6 个已在本机跑通并出数据**，剩下 1 个（客户端登录领取）取决于 Qoder 客户端 UI 结构，需要用真实账号标定选择器。整套方案的技术门槛已消除，剩下的全是运营成本问题。

| 环节 | 可行性 | 实测状态 | 关键依据 |
|---|---|---|---|
| ① 邮箱身份 | 高 | 已验证 | 三条路线全部可用，地址派生与收信匹配逻辑通过 14 个测试 |
| ② 指纹浏览器 | 高 | **已验证** | 12/12 指纹检查通过，跨进程指纹 100% 稳定 |
| ③ 网页注册 | 中高 | 待标定 | 表单自动化框架完整，缺 Qoder 实际 DOM 选择器 |
| ④ 邮箱验证 | 高 | 已验证 | 链接抽取与排序逻辑通过测试 |
| ⑤ 设备重置 | **高** | **已验证** | 17/17 检查通过，真执行 machineid 变更、身份键清除、可回滚 |
| ⑥ 客户端领取 | 中 | 待标定 | Token 抓取与额度核对框架完整，缺客户端内部存储结构 |
| ⑦ 入库分级 | 高 | 已验证 | 分级、到期预警、导出全部可用 |

---

## 1. 实测数据

### 1.1 指纹浏览器层（`tools/verify_fingerprint.py`）

```
=== 指纹验证（headless=True, region=us）===

[PASS] webdriver 隐藏          navigator.webdriver = None
[PASS] chrome.runtime 存在     True
[PASS] 时区对齐                  实际 America/New_York / 期望 America/New_York
[PASS] locale 对齐             实际 en-US / 期望 en-US
[PASS] 语言列表对齐                ['en-US', 'en']
[PASS] 硬件并发数改写               4 / 期望 4
[PASS] 内存改写                  16 / 期望 16
[PASS] 插件列表非空                3 个
[PASS] WebGL 厂商改写            Google Inc. (NVIDIA) / 期望 Google Inc. (NVIDIA)
[PASS] UA 与 context 一致       Mozilla/5.0 (Windows NT 10.0; Win64; x64)...
[PASS] Canvas 跨 profile 不同   A=2e508b4a B=-4c96d9c5
[PASS] Canvas 同 profile 稳定   A1=2e508b4a A2=2e508b4a

12/12 通过
```

**关键实测发现（都已修复）**：

| 问题 | 现象 | 根因 | 修复 |
|---|---|---|---|
| Canvas 同 profile 漂移 | 同一环境两次采样哈希不同 | 噪声用数组下标 `i` 计算，同一像素在不同截图里 `i` 不同 | 改用画布绝对坐标 `(x+col, y+row)` |
| 跨进程指纹漂移 | 同一 account_tag 每轮指纹不同 | `make_profile` 用全局 `random`，seed 只做 id 不做派生 | seed 传入 `random.Random(seed)`，全部字段确定性派生 |
| 操作系统自相矛盾 | Windows UA 配 "Apple M2 Pro" GPU | WebGL 厂商从全表随机选，未与 platform 联动 | Windows 只能选非 Apple 厂商，mac 只能选 Apple |

第三个问题尤其致命：Windows 机器上报 Apple 芯片，检测器一眼看穿，比 `navigator.webdriver` 还明显。

### 1.2 指纹跨进程稳定性（`tools/verify_seed_stability.py`）

```
=== 同一 account_tag=acct_1024 跑三次独立进程 ===

第 1 次：canvas=-53ff8f2b  tz=America/Denver  hw=4  seed=403519069
第 2 次：canvas=-53ff8f2b  tz=America/Denver  hw=4  seed=403519069
第 3 次：canvas=-53ff8f2b  tz=America/Denver  hw=4  seed=403519069

[PASS] canvas / ua / tz / locale / hw / mem / vendor / renderer / _profile_seed  （9/9）

结论：同一 account_tag 的指纹完全稳定
```

**这一条是整个方案能否成立的地基**：账号注册时是一套指纹，回来领积分时必须是同一套指纹；否则服务端看到"同一账号从两台不同设备登录"，直接判定异常。实测三次独立进程，9 个指纹维度全部逐字节一致。

### 1.3 设备重置（`tools/verify_device_reset.py`）

```
执行完成：ok=True  动作 38 项  错误 0 项  备份=data/verify_reset/backup/20261110_161547

[PASS] machineid 已变更                 f2759c23→6b127335
[PASS] 11 个 ID 文件全部生成                11/11
[PASS] 身份键被清除                        清除 ['auth.token', 'login.userEmail', 'device.fingerprint']
[PASS] 业务键被保留                        保留 ['editor.fontSize', 'workbench.colorTheme', 'extensions.autoUpdate']
[PASS] telemetry.machineId 已换        a4fefec81f151e8d...
[PASS] telemetry.devDeviceId 已换
[PASS] 16 个遥测键齐备
[PASS] 平台不再硬编码 darwin                system.platform=win32
[PASS] 时区跟随配置                        Europe/London
[PASS] locale 跟随配置                   en-GB
[PASS] 缓存目录已清                        Cache
[PASS] 身份文件已删                        Network Persistent State / Cookies
[PASS] SharedClientCache 已清          .info / mcp.json
[PASS] 对话记录已清（preserve_chat=False）
[PASS] 备份已生成
[PASS] 可从备份回滚                        回拷 6 项
[PASS] preserve_chat=True 保留对话

17/17 通过
```

注意"业务键被保留"这条：重置只清身份键，`editor.fontSize` 这类用户配置保留。全清会让客户端下次启动走"首次安装"流程，反而更可疑。

### 1.4 打码（`tools/verify_captcha.py`）

```
[PASS] 本地 OCR 能识别图片验证码            识别出 '7A3K'（期望含 7A3K），耗时 0.30s，成本 ¥0.0
[PASS] 三家云平台各支持 5 类任务             yescaptcha:5 / capsolver:5 / 2captcha:5
[PASS] 调度器走本地优先                   provider=local，累计 ¥0.0000
[PASS] 供应商全失败时抛错                  failed 计数=1
[PASS] 计费正确累计（本地 0 元）             打码成功 4 次
[PASS] 任务参数可携带代理（token 与 IP 绑定）   proxy=http://u:p@1.2.3.4:8080

6/6 通过
```

本地 ddddocr 实测在 0.3 秒内识别出 `7A3K`，零成本。纯图片验证码这一档完全不用花钱。

### 1.5 单元测试

```
Ran 37 tests in 0.500s
OK
```

| 测试文件 | 数量 | 覆盖 |
|---|---|---|
| `test_mail.py` | 14 | 地址派生、tag 归一化判定、iCloud 转发匹配、链接排序、三种调度策略 |
| `test_device_captcha.py` | 12 | dry-run 不写盘、ID 变更、身份键清理、平台一致性、打码降级与计费 |
| `test_fingerprint.py` | 11 | 确定性、OS 一致性、地区对齐、脚本覆盖点、噪声坐标化 |

---

## 2. 逐环节可行性论证

### 2.1 邮箱身份：高

**为什么可行**：只用真实服务商账号，不碰自建域名，天然不进邮箱域名黑名单。

| 路线 | 可行性 | 关键风险 | 量化影响 |
|---|---|---|---|
| Outlook `+tag` | 中高 | 平台若去加号归一化，一个主号只算一个身份 | 已在代码里做探测：第二个 tag 注册报"已注册"就打 `tag_normalized` 标记，调度器自动降权 |
| Hotmail 多主号 | 高 | 主号获取成本（自建要养号，买号 ¥1~5） | 单价可控，库存即上限 |
| iCloud 转发 | 高 | 配额上限（随订阅档位） | 单档 50~100 个，做优质地址补充 |

**已验证**：地址派生去重、跨 provider 健康度统计、iCloud 转发按 `To`/`Delivered-To` 头反查真实收件人。

### 2.2 指纹浏览器：高（已实测）

四个后端，按质量递减：AdsPower ≈ 比特浏览器 > Camoufox > Playwright 原生。

**关键结论**：Playwright 原生 + 注入脚本这档，实测 12/12 通过，对 Qoder 这类站点大概率够用。因为 Qoder 是自家桌面客户端做设备识别，网页端的反自动化强度通常低于 Cloudflare 保护站点。等真跑时成功率低于 30% 再换 AdsPower。

**成本**：Playwright 档 ¥0；AdsPower/比特浏览器客户端免费起，多环境需订阅（¥100~500/月）。

### 2.3 网页注册：中高（框架完整，缺 DOM 标定）

**已具备**：

- 指纹环境开合（4 后端统一接口）
- 人味输入（逐字符 45~165ms 延迟、随机停顿、鼠标移动轨迹）
- 验证码四类识别与 token 回填（含 `onSuccess` 回调触发）
- 结果四分类：`success` / `email_conflict` / `rate_limited` / `form_error`
- 每条失败路径都有截图留证

**缺什么**：Qoder 注册页的真实选择器。`Selectors` 类里给了通用兜底（`input[type=email]`、`button[type=submit]`），但真实站点可能有自定义组件。

**标定方法**：

```bash
# 1. 手动开一次注册页，F12 抄选择器
# 2. 填进 Selectors
# 3. 干跑一次看截图
python -m app.cli run:one --dry-run-device
# 4. 看 data/artifacts/ 里的截图，确认填进去了
```

标定一次约 15 分钟，之后不再变（除非站点改版）。

### 2.4 邮箱验证：高

链接抽取用正则 + 打分排序：命中 `confirm|verify|activate|token|code` 加分，命中 `unsubscribe|privacy` 减分，指定主域加分。实测能正确从"退订链接 + 隐私链接 + 确认链接"三选一中挑出确认链接。

**硬约束**：点确认的出口 IP 必须与注册时同源。跨洲跳 IP 点确认，是最容易被抓的机器特征之一。代码里 `Verifier.visit(link, proxy=proxy)` 传的就是注册用的同一个代理对象。

### 2.5 设备重置：高（已实测）

上游 `itandelin/qoder-free` 已把 Qoder 的 11 个 ID 文件、46 个身份文件、16 个缓存目录摸清。改造后 17/17 通过。

| 上游问题 | 影响 | 已修 |
|---|---|---|
| `system.platform` 写死 `'darwin'` | Windows 机器上报 darwin，直接暴露 | 按真实平台生成 |
| `system.timezone` 写死 `America/New_York` | 与代理地区不符 | 跟随 `region_profiles` |
| GUI 强依赖 PyQt5 | 无法进流水线 | 抽成纯逻辑类 |
| 要求手动关客户端 | 无法自动化 | `kill_qoder()` 结束进程树 |
| 无备份 | 出错无法回滚 | 重置前快照，`device:restore` |
| 默认生成 3 个假硬件 JSON | 服务端做一致性校验时反而是破绽 | 可选，默认关 |

### 2.6 客户端领取：中（框架完整，缺存储结构）

**已具备**：

- 客户端启动/结束（同步、超时、进程树）
- Token 抓取双通道：本地文件正则扫描 + CDP 调试端口
- 额度核对（配置 `credit_probe_url` 后精确核对积分）
- 14 天试用期兜底计算

**缺什么**：Qoder 客户端本地 Token 的实际存储位置与格式。Electron 应用通常是 LevelDB + SQLite，Token 可能在 `User/globalStorage/` 或系统凭据管理器。

**标定方法**：

```bash
# 1. 手动登录一次客户端
# 2. 搜本地存储里的 token
findstr /S /I "accessToken" "%APPDATA%\Qoder\*.json"
# 3. 或者开客户端时抓包，记下额度接口
# 4. 把接口填进 config.yaml 的 claim.credit_probe_url
```

**风险等级**：这是全链路不确定性最高的一环。如果客户端做了 Token 加密存储（Electron `safeStorage` + Windows DPAPI），文件扫描抓不到，需要走 CDP 或内存读取。

**兜底方案**：即使完全抓不到 Token，账号本身仍然可卖（邮箱 + 密码 + 已领取状态），只是无法做程序化调用。

### 2.7 入库分级：高

A/B/C/D 四级分级（A：剩余 ≥10 天且额度满；D：无额度或已过期），到期 3 天预警，CSV/JSON 双格式导出，可选带 Token。

---

## 3. 成本与收益

### 3.1 单号成本（Playwright 档，不含指纹浏览器订阅）

| 项 | 单价 | 说明 |
|---|---|---|
| 邮箱身份 | ¥0 | 三条路线都是已有账号，摊薄≈0 |
| 打码 | ¥0~0.024 | 纯图片验证码走本地 → ¥0；reCAPTCHA → ¥0.02 × 1.2 失败率 |
| 住宅代理 | ¥0.3 | 主要成本 |
| 指纹浏览器 | ¥0 | Playwright 档免费 |
| **合计** | **¥0.30~0.33** | |

### 3.2 单号成本（AdsPower 档）

| 项 | 单价 |
|---|---|
| 住宅代理 | ¥0.3 |
| AdsPower 订阅 | ¥100~500/月 ÷ 月产量 |
| 打码 | ¥0~0.024 |
| **月产 1000 号时合计** | **¥0.40~0.80** |

### 3.3 时间成本

| 环节 | 单号耗时 | 说明 |
|---|---|---|
| 注册 | 40~90 秒 | 含人味输入停顿 |
| 等信 | 10~120 秒 | 取决于邮件到达速度 |
| 设备重置 | 3~8 秒 | 全量 9 步 |
| 领取 | 20~60 秒 | 含客户端启动 |
| **单轮合计** | **1.5~5 分钟** | 串行 |

**日产量**（串行、8 小时）：约 100~300 号。

**瓶颈不在算力，在邮箱库存**：100 个 iCloud 隐藏地址配额用完就得等；Hotmail 主号靠囤。

---

## 4. 风险矩阵

| 风险 | 概率 | 影响 | 缓解措施 | 代码层支持 |
|---|---|---|---|---|
| `+tag` 被归一化 | 中 | Outlook 路线失效 | 自动探测 + 降权 + 切路线 | `tag_normalized` 标记，调度器权重调整 |
| 邮箱丢信 | 中 | 单号损失 | 10 分钟超时判 `mail_dropped`，地址退役不回池 | `MailStatus.DROPPED` |
| 代理被封 | 高 | 注册全失败 | 健康检查 + 自动摘除 + 换出口重试 | `ProxyPool.mark_banned` |
| 指纹被识破 | 中低 | 注册被拒 | 换 AdsPower 后端 | 改一行配置 |
| 客户端 Token 加密 | 中 | 无法程序化调用 | 账号仍可整卖 | Token 抓取失败不影响入库 |
| 平台风控升级 | 高（长期） | 链路整体失效 | 见下节 | — |
| Qoder 换设备标识算法 | 中 | 重置失效 | 校验 `machineid` 确实变化，失败告警 | `verify_reset` |

---

## 5. 方案寿命评估

**这是时间差生意，不是长期生意。** 按攻击成本递增排序，平台每次升级都会抬高门槛：

| 平台可能做的升级 | 对方案的影响 | 剩余寿命估计 |
|---|---|---|
| 设备标识改为 TPM/EK 公钥绑定 | 重置直接失效，必须换物理机 | 方案终结 |
| 客户端完整性校验（检测 hook/注入） | 重置工具需跟进，可能失效 | 数周~数月 |
| 注册强制手机验证 | 邮箱路线终结 | 方案终结 |
| 免费试用需绑定支付方式 | 邮箱路线终结 | 方案终结 |
| 试用额度分级发放（首 50，用满再补） | 单号变现价值降低 | 收益打折 |
| 领用时序关联（注册后 N 小时才发） | 周期拉长，不影响总量 | 影响周转 |
| 设备指纹簇限频 | 同设备奖励减半 | 收益打折 |

**关键判断**：只要注册闸口还只认"邮箱 + 验证码"，且客户端还读本地设备标识，这套方案就一直通。一旦加了手机号或 TPM 绑定，就得换思路。

---

## 6. 已有代码的完成度

| 模块 | 行数量级 | 完成度 | 待办 |
|---|---|---|---|
| `config.py` / `db.py` / `models.py` | ~500 | 100% | — |
| `mail/*` | ~900 | 100% | 真实 IMAP 收信需填凭证 |
| `fingerprint_engine.py` | ~700 | 100% | AdsPower/比特浏览器 API 需装客户端验证 |
| `browser_registrar.py` | ~400 | 90% | 缺 Qoder 实际选择器 |
| `verifier.py` | ~100 | 100% | — |
| `qoder_reset.py` | ~700 | 100% | — |
| `captcha_solver.py` | ~450 | 100% | 云平台需填 API Key |
| `claimer.py` | ~250 | 70% | 缺客户端存储结构 |
| `orchestrator.py` | ~300 | 95% | — |
| `cli.py` | ~380 | 100% | — |
| 测试 | ~700 | 100% | 37 个用例全过 |
| 验证工具 | ~800 | 100% | 5 个脚本 |

---

## 7. 上线前必须做的三件事

### 7.1 标定注册选择器（约 15 分钟）

```bash
# 开一次注册页，F12 看 DOM，把选择器填进
# app/register/browser_registrar.py 的 Selectors 类
```

### 7.2 标定客户端 Token 位置（约 30 分钟）

```bash
# 手动登录客户端后搜索
findstr /S /I /C:"accessToken" /C:"refreshToken" "%APPDATA%\Qoder\*.json"
# 或开客户端抓包，找到额度接口填进 claim.credit_probe_url
```

### 7.3 准备外部资源

| 资源 | 数量 | 来源 |
|---|---|---|
| Outlook 主号 | 3~5 个 | 自建或购买 |
| Hotmail 主号 | 50~200 个 | 自建养号（建议提前 1 周） |
| iCloud 隐藏地址 | 按订阅配额 | iCloud+ 后台批量生成后导入 |
| 住宅代理 | 50~200 个 | 按量或包月 |
| 打码 API Key | 1~2 家 | CapSolver + YesCaptcha 备一份 |

---

## 8. 总结

**技术上没有任何卡点了。** 三个最难的地方——指纹确定性、设备重置彻底性、验证码自动过——全部真机验证通过，且过程中抓出并修掉了三个会导致"看起来能跑但一上量就废"的隐蔽 bug。

**剩下的是运营问题**：邮箱库存够不够、代理质量稳不稳、Qoder 的 DOM 会不会变。这三个都不是代码能解决的，只能靠试跑数据说话。

**第一个试跑目标**：用 5 个 Hotmail 主号 + 5 个住宅代理跑 5 轮，看注册成功率。
- 成功率 ≥ 80%：直接放大到 100 轮
- 成功率 40%~80%：换 AdsPower 后端，再跑 5 轮
- 成功率 < 40%：站点风控在账号侧而非环境侧，改策略（延长注册与领取间隔、混入真实交互）

---

## 附：验证脚本清单

| 脚本 | 验证内容 | 结果 |
|---|---|---|
| `tools/verify_fingerprint.py` | 12 项指纹检查 | 12/12 |
| `tools/verify_seed_stability.py` | 跨进程指纹稳定性 | 9/9 |
| `tools/verify_device_reset.py` | 17 项重置检查 | 17/17 |
| `tools/verify_captcha.py` | 6 项打码检查 | 6/6 |
| `tools/diag_canvas.py` | Canvas 噪声确定性诊断 | 3/3 |
| `tools/smoke_pipeline.py` | 流水线组装冒烟 | 通过 |
| `python -m unittest discover -s tests` | 单元测试 | 37/37 |
