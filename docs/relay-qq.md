# iCloud+ 隐藏地址 → QQ 邮箱聚合收信

## 链路

```
Qoder 发验证码到 abcd1234@privaterelay.appleid.com   （iCloud「隐藏我的邮件」）
        ↓  Apple 转发
你的 QQ 邮箱（唯一聚合收件箱，IMAP: imap.qq.com:993）
        ↓  一个 IMAP 会话读全部
按「收件人 = 哪个隐藏地址」分发 → 提取 6 位验证码 → 回填注册页
```

几百个隐藏地址只用一个 IMAP 会话收信，关键就在**按收件人反查**。

## 需要你填的两处

### 1. 隐藏地址清单 `data/icloud_relay.txt`

一行一个，三种写法都认：

```
abcd1234@privaterelay.appleid.com
efgh5678                      # 只写用户名，自动补 @privaterelay.appleid.com
ijkl9012@privaterelay.appleid.com   # 备注写在 # 后面
```

`#` 之后忽略，空行跳过，大小写去重。

### 2. QQ 邮箱 IMAP（`config/config.yaml` 或 `config/secrets.yaml`）

```yaml
mail:
  icloud_relay:
    relay_addresses_file: "data/icloud_relay.txt"
    quota_total: 500
    aggregator_imap:
      host: "imap.qq.com"
      port: 993
      user: "12345678@qq.com"      # ← 你的 QQ 邮箱
      password: "env:QQ_IMAP_PASS" # ← QQ「授权码」，不是登录密码
      folder: "INBOX"
```

授权码位置：QQ 邮箱 → 设置 → 账号 → 「POP3/IMAP/SMTP 服务」→ 开启 IMAP → 生成授权码。

也可以用环境变量或 `config/secrets.yaml`：

```yaml
mail:
  icloud_relay:
    aggregator_imap:
      user: "12345678@qq.com"
      password: "xxxxxxxxxxxxxxxx"     # 16 位授权码
```

读取优先级：**环境变量 `QQ_IMAP_USER` / `QQ_IMAP_PASS` > `config/secrets.yaml` > 配置文件**。

## 匹配规则（可调）

```yaml
    match:
      sender_allow: ["qoder.com"]        # 发件人包含这些域
      subject_contains: ["qoder"]        # 主题或正文命中
      code_regex: "\\b(\\d{6})\\b"       # 验证码正则，取第一个分组
      scan_body_for_recipient: true      # 正文里也找隐藏地址（转发链常写页脚）
      lookback: 60                       # 每次轮询只看最近 N 封
      extra_recipient_headers: []        # 追加要扫的头，如 ["X-Forwarded-To"]
```

收件人匹配扫描的头：`To / Cc / Delivered-To / X-Original-To / Envelope-To /
X-Envelope-To / Apparently-To / Return-Path / Reply-To`，加上正文。
这样无论 Apple 把原地址留在哪个头里都能抓到。

## 联调步骤

```powershell
cd D:\qoder_register
$env:PYTHONPATH='D:\qoder_register;D:\qoder_register\.pylibs'

# 1) 先看地址池解析对不对
python tools/relay_check.py --pool

# 2) 连 QQ IMAP，看箱子里有多少信、最近几封的收件人能不能对上隐藏地址
python tools/relay_check.py

# 3) 查单个地址有没有码
python tools/relay_check.py --address abcd1234@privaterelay.appleid.com

# 4) 监听 5 分钟，实时报告哪个地址收到码（验证转发链路）
python tools/relay_check.py --watch 300
```

## 用它跑注册

```powershell
# 从池里自动取一个地址注册
python tools/run_signup_cdp.py --mail-source relay --code-timeout 600

# 指定某个隐藏地址
python tools/run_signup_cdp.py --mail-source relay ^
    --relay-address abcd1234@privaterelay.appleid.com --code-timeout 600
```

原来的 Outlook 清单路线不变：`--mail-source outlook --index N`。

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `LOGIN failed` / `AUTHENTICATE` 失败 | 用了登录密码 | 换成 IMAP 授权码 |
| 连不上 / 超时 | QQ 邮箱没开 IMAP 服务 | 设置里开启 IMAP/SMTP |
| 箱子里有信但匹配不到地址 | 转发时原地址只在正文页脚 | `scan_body_for_recipient: true`（默认已开） |
| 匹配到别的地址的码 | `lookback` 太大、正文里有多个地址 | 调小 `lookback`，或关掉正文扫描只认头 |
| 收不到任何信 | iCloud+ 转发没配到该 QQ 邮箱 | 先在 iCloud 设置里确认转发目标 |
