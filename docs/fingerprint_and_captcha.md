# 指纹浏览器与打码平台方案

> 配套文档：`docs/architecture.md`（总架构）、`docs/report.md`（调查报告）
> 代码：`app/register/fingerprint_engine.py`、`app/net/captcha_solver.py`、`app/register/browser_registrar.py`

---

## 1. 为什么注册阶段必须用指纹浏览器

Qoder 注册走网页，服务端拿到的所有环境信号里，能跨会话稳定识别"同一台机器"的有三类：

| 类别 | 典型信号 | 换 IP 能否规避 | JS 注入能否规避 |
|---|---|---|---|
| 网络指纹 | IP 归属、ASN、JA3/JA4、HTTP/2 帧指纹 | 部分 | 否（在 TLS 层） |
| 浏览器指纹 | Canvas/WebGL/Audio 哈希、字体列表、时区、UA、硬件并发数 | 否 | 不稳定（多次采样会跳动） |
| 账号侧关联 | 注册时间聚集、密码相似度、领用时序 | 否 | 否 |

只换代理不换指纹 → 指纹相同、IP 不同 = 高度可疑。
只换指纹不换 IP → IP 复用、指纹不同 = 中等可疑（多设备共用一个 NAT 可解释，但批量就露）。
两者同时换，且指纹与 IP 地理一致 → 才像真实的多台机器多地点用户。

所以注册阶段的最小可用组合是：**指纹浏览器环境 + 住宅代理 + 地区一致性**。

---

## 2. 四个后端对比

| 后端 | 内核 | 指纹质量 | 成本 | 适用场景 | 代码类 |
|---|---|---|---|---|---|
| AdsPower | 自编译 Chromium | 高：Canvas/WebGL/Audio/字体/时区全维度可配 | 客户端免费起，多环境要订阅 | 跑量主力 | `AdsPowerEngine` |
| 比特浏览器 | 自编译 Chromium | 高，与 AdsPower 同级，国内网络更稳 | 客户端免费起，环境数分档 | 国内环境主力 | `BitBrowserEngine` |
| Camoufox | 改造 Firefox（C++ 层） | 高，但 Firefox 的 TLS/JA3 与 Chrome 不同 | 开源免费 | 目标站点对 Firefox 不敏感时 | `CamoufoxEngine` |
| Playwright 原生 | 官方 Chromium + 注入脚本 | 中低：注入层可被高级检测识破 | 免费 | 跑通链路、自测、非对抗页面 | `PlaywrightFallbackEngine` |

选择规则（写在 `config.yaml` 的 `browser.backend`）：

1. 先跑 `env:check`，看目标后端健康状态。
2. 跑 5 个号做 A/B：分别用 `adspower` 和 `playwright`，比注册成功率。
3. 成功率差距 > 20 个百分点 → 继续用指纹浏览器；差距 < 5 个百分点 → 该站风控只在账号侧，用哪个都行。

---

## 3. 指纹与代理地区对齐（关键，别省）

服务端最容易抓的矛盾就是"IP 在美国，浏览器时区是 Asia/Shanghai"。本框架在三处强制对齐：

| 位置 | 做法 |
|---|---|
| 浏览器 context | `timezone_id` / `locale` 取自 `region_profiles.<region>` |
| HTTP 头 | `Accept-Language` 与 locale 同源 |
| 系统指纹（客户端侧） | `device:reset --timezone` 与注册时地区一致 |

`region_profiles` 默认表：

| region | timezone | locale | 屏幕候选 |
|---|---|---|---|
| us | America/New_York / Chicago / Denver / Los_Angeles | en-US | 1920×1080 / 2560×1440 / 1536×864 / 1366×768 |
| uk | Europe/London | en-GB | 1920×1080 / 2560×1440 / 1440×900 |
| jp | Asia/Tokyo | ja-JP | 1920×1080 / 2560×1440 / 1366×768 |
| sg | Asia/Singapore | en-SG | 1920×1080 / 2560×1440 |
| de | Europe/Berlin | de-DE | 1920×1080 / 2560×1440 / 1680×1050 |
| hk | Asia/Hong_Kong | zh-HK | 1920×1080 / 2560×1440 / 1366×768 |

一个 region 下有多条候选时随机选，保证同一地区的多个账号不撞同一个组合。

---

## 4. 注入脚本覆盖点（Playwright 兜底后端）

`stealth_init_script()` 在页面脚本之前执行，覆盖：

| 信号 | 处理 |
|---|---|
| `navigator.webdriver` | 置 `undefined` |
| `window.chrome` | 补 `runtime` / `loadTimes` / `csi` |
| `navigator.languages` / `language` | 与 locale 一致 |
| `navigator.platform` | Win32 / MacIntel |
| `navigator.hardwareConcurrency` / `deviceMemory` | 按画像给定 |
| `navigator.plugins` / `mimeTypes` | 补齐 PDF 三件套 |
| WebGL `UNMASKED_VENDOR_WEBGL` / `UNMASKED_RENDERER_WEBGL` | 按画像返回厂商与具体显卡串 |
| Canvas `getImageData` | 确定性噪声（同 profile 稳定，跨 profile 不同） |
| Audio `getFloatFrequencyData` | 微幅噪声 |
| `permissions.query` | 修正 notifications 返回值 |
| `Intl.DateTimeFormat.resolvedOptions` | 时区与 locale 强制一致 |

噪声用**确定性哈希**而不是 `Math.random()`：同一个环境多次采样值稳定，跨环境不同。用随机数会导致"同一设备两次采样不一致"，这是检测器最爱的破绽。

---

## 5. 打码平台方案

### 5.1 供应商与任务覆盖

| 供应商 | reCAPTCHA v2 | v3 | hCaptcha | Turnstile | 图片 | 单价量级 |
|---|---|---|---|---|---|---|
| YesCaptcha | ✅ | ✅ | ✅ | ✅ | ✅ | ¥0.0015~0.006 |
| CapSolver | ✅ | ✅ | ✅ | ✅（含 DataDome） | ✅ | ¥0.005~0.02 |
| 2Captcha | ✅ | ✅ | ✅ | ✅ | ✅ | ¥0.01~0.03 |
| 本地 ddddocr | — | — | — | — | ✅ | ¥0 |

### 5.2 路由与降级

`captcha.prefer` 按任务类型排优先级，前一个失败自动降级到下一个：

```yaml
captcha:
  prefer:
    image: [local, yescaptcha, "2captcha"]          # 先白嫖本地
    recaptcha_v2: [capsolver, yescaptcha, "2captcha"]
    recaptcha_v3: [capsolver, yescaptcha, "2captcha"]
    hcaptcha: [capsolver, yescaptcha, "2captcha"]
    turnstile: [capsolver, yescaptcha]
```

为什么 turnstile 只有两家：Turnstile 需要专门的浏览器环境模拟，能稳定出的供应商少，2Captcha 的通过率偏低。

### 5.3 任务识别与回填

`BrowserRegistrar._detect_captcha()` 按 iframe 源探测类型，优先级 turnstile → hcaptcha → reCAPTCHA v2 → 图片。
拿到 token 后回填方式：

| 类型 | 回填目标 |
|---|---|
| reCAPTCHA v2 | `#g-recaptcha-response` + `textarea[name=g-recaptcha-response]` |
| hCaptcha | `#h-captcha-response` + `textarea[name=h-captcha-response]` |
| Turnstile | `#cf-turnstile-response` + 触发 `onSuccess` 回调 |
| 图片 | 直接 `fill` 到输入框 |

同时触发 `onCaptchaSuccess` / `onSuccess` / `__turnstile_cb` 三个常见回调名，适应不同前端实现。

### 5.4 成本账

`CaptchaSolver` 每解决一次累加 `spent`，`run:loop` 结束打印：

```
打码成功 42 次，失败 3 次，累计 ¥0.1260，均次 ¥0.0030
```

单号打码成本 = `均次 × (1 + 失败率)`，失败率按 10%~20% 估。

### 5.5 代理与打码的联动

reCAPTCHA v2 的高分 token 与**出口 IP 强绑定**：用 A 代理取 token、用 B 代理提交，直接掉分。
框架做法：`CaptchaTask.proxy` 传的就是注册用的同一个 `Proxy.raw`，即"取 token 的 IP = 提交表单的 IP"。
带代理的任务用 `NoCaptchaTask` / `ReCaptchaV2Task`（非 ProxyLess 版本），单价略高但通过率高。

---

## 6. 完整注册调用链

```
Pipeline.run_one()
  ├── mail_layer.acquire(1)                    取邮箱
  ├── proxies.acquire(region=us)                取代理（优先最久未用）
  ├── engine.open(proxy, region, account_tag)   开指纹环境
  │     ├── AdsPower/BitBrowser: HTTP API 建环境 → 启动 → CDP 接管
  │     └── Playwright: launch + context + init_script
  ├── page.goto(signup_url)
  ├── 填表（模拟输入节奏、随机停顿、鼠标移动）
  ├── _detect_captcha → solver.solve → 回填
  ├── 提交 → _classify
  │     ├── email_conflict  → 打 tag_normalized 标记，调度器降权该路线
  │     ├── rate_limited    → 标代理 banned，换出口
  │     └── success         → 进入验证
  └── session.close()
```

---

## 7. 失败模式与对策表

| 现象 | 根因 | 对策 |
|---|---|---|
| 注册全部报 email_conflict | `+tag` 被归一化 | 降权 `outlook_tag`，切 hotmail / icloud |
| 注册报 rate_limited | 代理干净度不够 | 换住宅代理供应商，或加大 IP 池、降低单位时间请求 |
| 验证码反复失败 | siteKey 错 / 代理与 token 不匹配 | 核对 siteKey；确保取 token 与提交同 IP |
| 提交后无反应 | 前端在等 turnstile 回调 | 补 `onSuccess` 触发；或改用手动过一次 |
| 注册成功但收不到信 | 邮件被判垃圾 / 转发链丢信 | 换 provider；`mail_dropped` 标记，地址退役 |
| 领取时被判老设备 | 重置不彻底 | `device:reset --execute` 全量 9 步；校验 `machineid` 确实变化 |

---

## 8. 自检命令

```bash
# 后端、打码、代理、设备目录一次看全
python -m app.cli env:check

# 设备重置预演（只打印动作，不写盘）
python -m app.cli device:reset

# 只重置必要三样
python -m app.cli device:reset --execute --steps machine_id,telemetry,login

# 带地区对齐的全量重置
python -m app.cli device:reset --execute --timezone Europe/London
```
